"""Process-wide fleet runtime — the live integration seam (U10).

This is the glue the U10 capstone was missing: a single, process-wide
:class:`FleetRuntime` that the **running app** and the **voice tools** share so
that a manager spawned *by voice* actually shows up on the dashboard and the
robot body. One source of truth, many renderers (plan.md §3).

The disconnect U10's on-robot run exposed:

- ``main.run`` built the voice/gradio app but never mounted the ``/fleet``
  dashboard nor started a :class:`FleetPoller`, so ``GET /fleet`` 404'd and no
  state was ever refreshed; and
- the only voice coding tool (``ask_claude_code``) runs an *in-process*
  :class:`~reachy_fleet_supervisor.claude_brain.WorkerSession`, which is **not**
  a ``claude --bg`` agent and so never appears in ``claude agents --json`` — the
  exact roster the poller reads. The voice spawn path and the fleet
  observability path were simply not wired to each other.

This module fixes both by giving everyone one shared runtime:

- :func:`get_fleet_runtime` returns a lazily-built, started singleton
  (:class:`FleetState` + :class:`SessionManager` + a running :class:`FleetPoller`
  over the real ``claude agents --json`` roster).
- :func:`mount_fleet_dashboard` mounts the read-only dashboard (U7) onto the
  served app at ``/fleet`` against that same state — what ``main.run`` calls.
- the ``spawn_manager`` voice tool routes spawns through
  ``runtime.session_manager`` so the new ``claude --bg`` manager is roster-visible
  and the poller folds it into the shared :class:`FleetState` the dashboard and
  body already render.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional
from dataclasses import dataclass

from .manager import AgentInfo
from .state import DEFAULT_POLL_INTERVAL, FleetState, FleetPoller
from .status import default_status_dir
from .session_manager import AgentPredicate, SessionManager

logger = logging.getLogger(__name__)


def _is_background(agent: AgentInfo) -> bool:
    """Keep only ``--bg`` managers — the fleet's definition of a manager."""
    return agent.is_background


@dataclass
class FleetRuntime:
    """The shared, live fleet objects: one state, one registry, one poller.

    Renderers (dashboard / body / CLI) read :attr:`state`; the voice
    ``spawn_manager`` tool spawns through :attr:`session_manager`; the
    :attr:`poller` keeps :attr:`state` fresh from the real roster.
    """

    state: FleetState
    session_manager: SessionManager
    poller: FleetPoller

    def start(self) -> "FleetRuntime":
        """Start the background poll loop (idempotent)."""
        self.poller.start()
        return self

    def stop(self) -> None:
        """Stop the background poll loop (best-effort)."""
        try:
            self.poller.stop()
        except Exception:  # noqa: BLE001 — shutdown must never raise
            logger.debug("fleet runtime poller stop failed", exc_info=True)


def build_fleet_runtime(
    *,
    claude_bin: str = "claude",
    interval: float = DEFAULT_POLL_INTERVAL,
    predicate: Optional[AgentPredicate] = _is_background,
    fetch_logs: bool = True,
    include_all: bool = False,
    status_dir: Optional[str] = None,
    reconnect: bool = True,
) -> FleetRuntime:
    """Build (but do not start) a :class:`FleetRuntime`.

    Adopts any already-running background managers from the roster (so a restart
    re-attaches, plan.md §4 persistence) unless *reconnect* is ``False``. The
    poller keeps only background managers by default (*predicate*) so unrelated
    foreground sessions on the host are ignored.
    """
    state = FleetState()
    session_manager = SessionManager(claude_bin=claude_bin)
    if reconnect:
        try:
            adopted = session_manager.reconnect(include_all=include_all)
            if adopted:
                logger.info("fleet runtime adopted %d existing manager(s)", len(adopted))
        except Exception:  # noqa: BLE001 — a missing/locked CLI must not block startup
            logger.warning("fleet runtime: roster reconnect failed; starting empty", exc_info=True)
    poller = FleetPoller(
        state,
        interval=interval,
        predicate=predicate,
        fetch_logs=fetch_logs,
        include_all=include_all,
        claude_bin=claude_bin,
        status_dir=str(status_dir) if status_dir is not None else str(default_status_dir()),
        # U13: a drive-loop manager reports via status.json; fall back to its
        # transcript sentinel so a HUMAN_GATE flows into FleetState even before
        # the on-disk report lands. Needs the log tail (fetch_logs).
        derive_status_from_transcript=fetch_logs,
    )
    return FleetRuntime(state=state, session_manager=session_manager, poller=poller)


# ---------------------------------------------------------------------------
# Process-wide singleton
# ---------------------------------------------------------------------------

_RUNTIME_LOCK = threading.RLock()
_RUNTIME: Optional[FleetRuntime] = None
# Optional override of how the singleton is built (tests inject a fake builder).
_BUILDER: Optional[Callable[[], FleetRuntime]] = None


def get_fleet_runtime() -> FleetRuntime:
    """Return the process-wide runtime, building + starting it on first use.

    Both the served app (via :func:`mount_fleet_dashboard`) and the voice
    ``spawn_manager`` tool call this, so they share one :class:`FleetState`.
    """
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is None:
            builder = _BUILDER or build_fleet_runtime
            _RUNTIME = builder().start()
            logger.info("fleet runtime started (poller polling `claude agents --json`)")
        return _RUNTIME


def set_fleet_runtime(runtime: Optional[FleetRuntime]) -> None:
    """Inject or clear the process-wide runtime (tests / explicit teardown).

    Stops any previously-running poller so tests don't leak threads.
    """
    global _RUNTIME
    with _RUNTIME_LOCK:
        if _RUNTIME is not None and _RUNTIME is not runtime:
            _RUNTIME.stop()
        _RUNTIME = runtime


def mount_fleet_dashboard(
    server_app: Any,
    *,
    runtime: Optional[FleetRuntime] = None,
    path: str = "/fleet",
    controls: bool = True,
    **dashboard_kwargs: Any,
) -> FleetRuntime:
    """Mount the fleet dashboard (U7 + U12 controls) onto *server_app* at *path*.

    Uses *runtime* if given, else the process-wide :func:`get_fleet_runtime`
    (which also starts the poller). When *controls* is true (the default) the
    dashboard is interactive: it gets a :class:`FleetController` over the
    runtime's :class:`SessionManager` so send / pause / resume / kill work from
    the browser (U12). Pass ``controls=False`` for a read-only mount. Returns the
    runtime so the caller can stop it on shutdown. This is what ``main.run``
    calls to serve ``/fleet``.
    """
    from .control import FleetController
    from .dashboard import create_dashboard_app

    rt = runtime if runtime is not None else get_fleet_runtime()
    controller = FleetController(rt.session_manager) if controls else None
    server_app.mount(path, create_dashboard_app(rt.state, controller=controller, **dashboard_kwargs))
    logger.info("fleet dashboard mounted at %s (controls=%s)", path, controls)
    return rt
