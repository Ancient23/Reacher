"""Interactive fleet controls — the *write* side of the fleet (U12).

Phase 2 was deliberately read-only: FleetState + the CLI/dashboard/body only
*observe* managers (decision #21 — read status, never interrupt). U12 adds the
human steering controls Phase 3 needs, and gives the **headless CLI and the web
dashboard one shared implementation** so "approve a gate", "send a message",
"pause / resume", "kill" and "reassign" behave identically wherever they are
triggered.

:class:`FleetController` wraps a
:class:`~reachy_fleet_supervisor.fleet.session_manager.SessionManager` and turns
each action into a structured :class:`ControlResult` (never raising for an
expected error — a bad key or a remote-control manager comes back as
``ok=False`` with a human-readable ``detail``), so a dashboard endpoint can map
it straight to JSON and the CLI can map ``ok`` to an exit code.

The actions and the mechanism behind each (verified on Claude Code 2.1.196):

- ``send``   — steer a background manager via ``claude --resume <sessionId> -p
  <message>`` (the decision #16 path, :func:`build_steer_argv`). This single
  call IS "send a message", "approve / answer a gate" and "reassign" — they
  differ only in what you say. Remote-control managers are steered from
  claude.ai/mobile instead, so ``send`` reports that.
- ``pause``  — ``claude stop <id>`` (halt, conversation kept, resumable).
- ``resume`` — ``claude respawn <id>`` (restart a paused/exited bg manager).
- ``kill``   — ``claude stop`` + ``claude rm`` and forget it (spawn-kill's kill;
  *spawn* stays on :meth:`SessionManager.spawn`).
"""

from __future__ import annotations
import logging
from typing import Any, Optional
from dataclasses import field, dataclass

from .manager import FleetManagerError
from .session_manager import SessionManager


logger = logging.getLogger(__name__)

# The control verbs this surface understands (what the dashboard renders buttons
# for and what ``POST /api/control/<action>`` validates against).
CONTROL_ACTIONS: tuple[str, ...] = ("send", "pause", "resume", "kill")

# Steering replies can be long; keep the ``detail`` we surface to a UI bounded.
_MAX_DETAIL_CHARS = 4000


@dataclass
class ControlResult:
    """The outcome of one control action — JSON-ready, never an exception.

    ``ok`` is whether the action succeeded; ``detail`` is a human-readable
    message (the manager's reply for ``send``, a short confirmation otherwise, or
    the error text on failure).
    """

    ok: bool
    action: str
    key: str
    detail: str = ""
    manager_id: Optional[str] = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a plain dict for a JSON response."""
        data: dict[str, Any] = {
            "ok": self.ok,
            "action": self.action,
            "key": self.key,
            "detail": self.detail,
            "manager_id": self.manager_id,
        }
        if self.extra:
            data.update(self.extra)
        return data


def _truncate(text: str) -> str:
    """Bound a (possibly long) steering reply for display."""
    text = (text or "").strip()
    if len(text) <= _MAX_DETAIL_CHARS:
        return text
    return text[:_MAX_DETAIL_CHARS] + " …[truncated]"


class FleetController:
    """Interactive control surface over a :class:`SessionManager` (U12).

    Shared by the headless CLI and the web dashboard so the steering controls
    behave identically in both. Reads still go through :class:`FleetState`
    (decision #21); this is purely the write side.
    """

    def __init__(self, session_manager: SessionManager) -> None:
        """Wrap *session_manager* — the registry whose managers this controls."""
        self.session_manager = session_manager

    @property
    def actions(self) -> tuple[str, ...]:
        """The control verbs this controller accepts."""
        return CONTROL_ACTIONS

    # ---- internal helpers -------------------------------------------------

    def _resolve(self, action: str, key: str) -> tuple[Optional[Any], Optional[ControlResult]]:
        """Look up the manager for *key*; return ``(manager, None)`` or ``(None, err)``."""
        if not key or not str(key).strip():
            return None, ControlResult(False, action, key or "", "no manager key given")
        try:
            mgr = self.session_manager.get(key)
        except FleetManagerError as exc:  # ambiguous name
            return None, ControlResult(False, action, key, str(exc))
        if mgr is None:
            return None, ControlResult(False, action, key, f"no manager matching {key!r}")
        return mgr, None

    # ---- actions ----------------------------------------------------------

    def send(self, key: str, message: str, **kwargs: Any) -> ControlResult:
        """Steer a manager (send a message / approve a gate / reassign)."""
        mgr, err = self._resolve("send", key)
        if err is not None:
            return err
        assert mgr is not None
        if not message or not message.strip():
            return ControlResult(False, "send", key, "message must not be empty", mgr.id)
        try:
            proc = mgr.send_message(message, **kwargs)
        except FleetManagerError as exc:
            return ControlResult(False, "send", key, str(exc), mgr.id)
        reply = _truncate(proc.stdout if proc is not None else "")
        logger.info("steered manager '%s' (id=%s)", mgr.name, mgr.id)
        return ControlResult(True, "send", key, reply or "(no reply)", mgr.id)

    def pause(self, key: str) -> ControlResult:
        """Pause a manager (``claude stop``; conversation kept, resumable)."""
        mgr, err = self._resolve("pause", key)
        if err is not None:
            return err
        assert mgr is not None
        try:
            mgr.pause(check=False)
        except FleetManagerError as exc:
            return ControlResult(False, "pause", key, str(exc), mgr.id)
        return ControlResult(True, "pause", key, f"paused {mgr.name}", mgr.id)

    def resume(self, key: str) -> ControlResult:
        """Resume a paused/exited manager (``claude respawn``)."""
        mgr, err = self._resolve("resume", key)
        if err is not None:
            return err
        assert mgr is not None
        try:
            mgr.resume(check=False)
        except FleetManagerError as exc:
            return ControlResult(False, "resume", key, str(exc), mgr.id)
        return ControlResult(True, "resume", key, f"resumed {mgr.name}", mgr.id)

    def kill(self, key: str) -> ControlResult:
        """Stop, remove and forget a manager (spawn-kill's *kill*)."""
        mgr, err = self._resolve("kill", key)
        if err is not None:
            return err
        assert mgr is not None
        mid, mname = mgr.id, mgr.name
        try:
            self.session_manager.stop_and_remove(key)
        except (FleetManagerError, KeyError) as exc:
            return ControlResult(False, "kill", key, str(exc), mid)
        return ControlResult(True, "kill", key, f"killed {mname}", mid)

    def apply(self, action: str, key: str, message: str = "", **kwargs: Any) -> ControlResult:
        """Dispatch one *action* by name (the dashboard endpoint's entry point)."""
        if action == "send":
            return self.send(key, message, **kwargs)
        if action == "pause":
            return self.pause(key)
        if action == "resume":
            return self.resume(key)
        if action == "kill":
            return self.kill(key)
        return ControlResult(
            False, action, key, f"unknown action {action!r}; valid: {list(CONTROL_ACTIONS)}"
        )
