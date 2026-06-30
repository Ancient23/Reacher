"""SessionManager — track N background managers and reconnect on restart (U3).

A :class:`SessionManager` is the in-process registry of the fleet's manager
sessions. Each manager is one durable ``claude --bg`` session wrapped by a
:class:`~reachy_fleet_supervisor.fleet.manager.FleetManager` (U2). This layer
adds the *N* part and, crucially, **durability across app restarts**: because
background agents are supervisor-hosted (they outlive the app that spawned
them), a fresh ``SessionManager`` can rebuild its registry from the live daemon
roster via ``claude agents --json`` — see plan.md §4 (Phase 2, persistence) and
the verified fleet mechanics (Claude Code 2.1.195).

It deliberately does NOT orchestrate work — each manager orchestrates its own
subagents / Workflows / ralph-loops natively (plan.md §3). This is purely
*track + route*: spawn, look up (by short id or voice name), stop/remove, and
reconnect. Live status aggregation (polling) is U4 ``FleetState``; this module
only owns the set of handles.

The registry is keyed by the authoritative **short id**. A manager's ``name``
is the human/voice handle ("how's the unreal one doing?") and is kept unique on
spawn; lookups accept either. The class holds no subprocess state of its own —
every control call delegates to the wrapped :class:`FleetManager`.
"""

from __future__ import annotations
import logging
from typing import Any, Callable, Iterator, Optional, Sequence
from pathlib import Path

from .manager import (
    DEFAULT_RUN_MODE,
    DEFAULT_PERMISSION_MODE,
    AgentInfo,
    FleetManager,
    FleetManagerError,
    list_agents,
)


logger = logging.getLogger(__name__)

# A predicate over a roster row, used to scope which background sessions a
# SessionManager adopts on reconnect (e.g. only this fleet's cwd, or a name
# prefix) so it doesn't grab unrelated background agents on the machine.
AgentPredicate = Callable[[AgentInfo], bool]


class SessionManager:
    """Tracks N :class:`FleetManager` background sessions; reconnects on restart.

    Construct empty and :meth:`spawn` / :meth:`reconnect`, or use
    :meth:`from_roster` to build one already populated from the live daemon
    roster (the normal app-startup path).
    """

    def __init__(self, *, claude_bin: str = "claude") -> None:
        self.claude_bin = claude_bin
        # Keyed by short id (authoritative). Insertion order = spawn/adopt order.
        self._by_id: dict[str, FleetManager] = {}

    # ---- container protocol ----------------------------------------------

    def __len__(self) -> int:
        return len(self._by_id)

    def __iter__(self) -> Iterator[FleetManager]:
        return iter(self._by_id.values())

    def __contains__(self, key: object) -> bool:
        return isinstance(key, str) and self.get(key) is not None

    @property
    def managers(self) -> list[FleetManager]:
        """All tracked managers, in spawn/adopt order."""
        return list(self._by_id.values())

    def ids(self) -> list[str]:
        """Short ids of all tracked managers."""
        return list(self._by_id.keys())

    def names(self) -> list[str]:
        """Names (voice handles) of all tracked managers."""
        return [m.name for m in self._by_id.values()]

    # ---- lookup -----------------------------------------------------------

    def get(self, key: str) -> Optional[FleetManager]:
        """Return the manager whose short id OR name is *key*, else ``None``.

        Short id is matched first (it is unique). If several managers share a
        name (possible after adopting an externally-spawned roster), a name
        lookup is ambiguous and raises :class:`FleetManagerError` — use the id.
        """
        direct = self._by_id.get(key)
        if direct is not None:
            return direct
        matches = [m for m in self._by_id.values() if m.name == key]
        if len(matches) > 1:
            raise FleetManagerError(
                f"ambiguous manager name {key!r}: {len(matches)} managers share it; "
                f"use a short id instead ({[m.id for m in matches]})"
            )
        return matches[0] if matches else None

    def require(self, key: str) -> FleetManager:
        """Like :meth:`get` but raise ``KeyError`` if not tracked."""
        manager = self.get(key)
        if manager is None:
            raise KeyError(
                f"no tracked manager with id/name {key!r}; "
                f"tracked: {sorted(self._by_id.keys())}"
            )
        return manager

    # ---- registration -----------------------------------------------------

    def adopt(self, manager: FleetManager) -> FleetManager:
        """Register an existing :class:`FleetManager` handle (idempotent by id).

        Re-adopting the same short id replaces the stored handle. Used by
        :meth:`spawn` and :meth:`reconnect`.
        """
        self._by_id[manager.id] = manager
        return manager

    def forget(self, key: str) -> Optional[FleetManager]:
        """Drop a manager from the registry WITHOUT stopping it; return it if present."""
        manager = self.get(key)
        if manager is not None:
            self._by_id.pop(manager.id, None)
        return manager

    # ---- spawn ------------------------------------------------------------

    def spawn(
        self,
        task: str,
        *,
        name: str,
        cwd: Optional[str | Path] = None,
        session_id: Optional[str] = None,
        run_mode: str = DEFAULT_RUN_MODE,
        permission_mode: str = DEFAULT_PERMISSION_MODE,
        model: Optional[str] = None,
        mcp_configs: Sequence[str] = (),
        worktree: Optional[str | bool] = None,
        extra_args: Sequence[str] = (),
        **spawn_kwargs: Any,
    ) -> FleetManager:
        """Spawn a new manager (in *run_mode*) and track it.

        ``name`` must not collide with an already-tracked manager (it is the
        voice handle). ``run_mode`` selects the durable ``background`` backbone
        (default) or a ``remote-control`` steerable session. All other arguments
        pass through to :meth:`FleetManager.spawn` (e.g. ``launcher`` /
        ``remote_control_name_prefix`` for remote-control). The new handle is
        registered and returned.
        """
        if any(m.name == name for m in self._by_id.values()):
            raise FleetManagerError(
                f"a manager named {name!r} is already tracked; pick a unique name"
            )
        manager = FleetManager.spawn(
            task,
            name=name,
            cwd=cwd,
            session_id=session_id,
            run_mode=run_mode,
            permission_mode=permission_mode,
            model=model,
            mcp_configs=mcp_configs,
            worktree=worktree,
            extra_args=extra_args,
            claude_bin=self.claude_bin,
            **spawn_kwargs,  # e.g. timeout / resolve_retries / launcher
        )
        return self.adopt(manager)

    # ---- reconnect (the U3 headline) -------------------------------------

    def reconnect(
        self,
        *,
        include_all: bool = False,
        predicate: Optional[AgentPredicate] = None,
    ) -> list[FleetManager]:
        """Adopt live background sessions from the roster; return the NEW ones.

        This is how the app re-attaches to its durable managers after a restart:
        it reads ``claude agents --json`` and wraps every *background* row not
        already tracked. Interactive sessions are ignored (they are not fleet
        managers). Already-tracked ids are left untouched, so calling this
        repeatedly is safe (idempotent).

        ``include_all`` includes completed/stopped sessions still in the roster
        (``--all``) — useful on restart to re-adopt a manager that finished or
        is blocked. ``predicate`` scopes which rows are adopted (e.g. by ``cwd``
        or name prefix) so unrelated background agents on the machine are not
        pulled in.
        """
        adopted: list[FleetManager] = []
        for agent in list_agents(include_all=include_all, claude_bin=self.claude_bin):
            if not agent.is_background or agent.id is None:
                continue
            if agent.id in self._by_id:
                continue  # already tracked — keep the existing handle
            if predicate is not None and not predicate(agent):
                continue
            manager = FleetManager.from_agent_info(agent, claude_bin=self.claude_bin)
            self.adopt(manager)
            adopted.append(manager)
            logger.info(
                "reconnected to background manager '%s' (id=%s session=%s)",
                manager.name, manager.id, manager.session_id,
            )
        return adopted

    @classmethod
    def from_roster(
        cls,
        *,
        claude_bin: str = "claude",
        include_all: bool = False,
        predicate: Optional[AgentPredicate] = None,
    ) -> "SessionManager":
        """Build a :class:`SessionManager` already populated from the live roster."""
        session_manager = cls(claude_bin=claude_bin)
        session_manager.reconnect(include_all=include_all, predicate=predicate)
        return session_manager

    # ---- control ----------------------------------------------------------

    def stop(self, key: str, *, check: bool = True) -> None:
        """Stop one manager (``claude stop``); keep it tracked (state persists)."""
        self.require(key).stop(check=check)

    # ---- interactive steering (U12) --------------------------------------

    def send_message(self, key: str, message: str, **kwargs: Any) -> Any:
        """Steer one tracked manager (send / approve gate / reassign).

        Delegates to :meth:`FleetManager.send_message` (the decision #16
        ``claude --resume <id> -p`` path for background managers). Returns the
        completed process (its ``stdout`` is the manager's reply).
        """
        return self.require(key).send_message(message, **kwargs)

    def pause(self, key: str, *, check: bool = True) -> None:
        """Pause one tracked manager (``claude stop``; conversation kept, resumable)."""
        self.require(key).pause(check=check)

    def resume(self, key: str, *, check: bool = True) -> None:
        """Resume one paused/exited tracked manager (``claude respawn``)."""
        self.require(key).resume(check=check)

    def stop_and_remove(self, key: str, *, forget: bool = True) -> None:
        """Stop + remove one manager and (by default) forget it from the registry."""
        manager = self.require(key)
        manager.stop_and_remove()
        if forget:
            self._by_id.pop(manager.id, None)

    def stop_all(self, *, check: bool = False) -> None:
        """Stop every tracked manager; leave them tracked (they can be resumed)."""
        for manager in list(self._by_id.values()):
            manager.stop(check=check)

    def stop_and_remove_all(self) -> None:
        """Best-effort teardown of ALL tracked managers, then clear the registry."""
        for manager in list(self._by_id.values()):
            manager.stop_and_remove()
        self._by_id.clear()
