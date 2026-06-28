"""Fleet supervisor core (Phase 2+).

Holds the project-agnostic fleet model that the SessionManager, FleetState,
CLI, dashboard and robot body all build on.

- U1 adds the fleet *config* layer: a typed, validated description of the
  projects a manager can be pointed at plus the default gate policy.
- U2 adds :class:`FleetManager`: spawn + control of one ``claude --bg``
  background manager session (the fleet's durable substrate). See plan.md §4
  (Phase 2) and decisions #11, #13, #19.
- U3 adds :class:`SessionManager`: tracks N managers and reconnects to existing
  background sessions from the daemon roster on app restart.
- U4 adds :class:`FleetState` (single source of truth, pub/sub) and
  :class:`FleetPoller`: aggregate per-manager state by polling
  ``claude agents --json`` (+ logs) and notify subscribers on change.
- U5 adds the *status convention* (:class:`ManagerStatus`, :func:`write_status`,
  :func:`read_status`): each manager emits its own drive-loop report to a known
  place and FleetState merges it — read status, never interrupt (decision #21).
"""

from __future__ import annotations

from .config import (
    RunMode,
    GateMode,
    GatePolicy,
    FleetConfig,
    ProjectConfig,
    McpServerConfig,
    ProjectDefaults,
    FleetConfigError,
    load_fleet_config,
    parse_fleet_config,
)
from .manager import (
    DEFAULT_PERMISSION_MODE,
    AgentInfo,
    FleetManager,
    FleetManagerError,
    list_agents,
    short_id_for,
    build_spawn_argv,
    parse_agents_json,
    parse_spawn_output,
    resolve_claude_bin,
)
from .session_manager import (
    AgentPredicate,
    SessionManager,
)
from .state import (
    ATTENTION_STATES,
    DEFAULT_LOG_LINES,
    DEFAULT_POLL_INTERVAL,
    FleetPoller,
    FleetState,
    FleetSnapshot,
    ManagerSnapshot,
    Subscriber,
    tail_logs,
)
from .status import (
    ATTENTION_SENTINELS,
    SENTINEL_COMPLETE,
    SENTINEL_CONTINUE,
    SENTINEL_FAILED,
    SENTINEL_HUMAN_GATE,
    SENTINEL_RUNNING,
    STATUS_DIR_ENV,
    STATUS_FILENAME,
    ManagerStatus,
    default_status_dir,
    parse_sentinel,
    read_status,
    read_status_for,
    read_statuses_for_agents,
    status_path_for,
    write_status,
)


__all__ = [
    # config (U1)
    "RunMode",
    "GateMode",
    "GatePolicy",
    "McpServerConfig",
    "ProjectDefaults",
    "ProjectConfig",
    "FleetConfig",
    "FleetConfigError",
    "load_fleet_config",
    "parse_fleet_config",
    # manager (U2)
    "FleetManager",
    "FleetManagerError",
    "AgentInfo",
    "list_agents",
    "short_id_for",
    "build_spawn_argv",
    "parse_spawn_output",
    "parse_agents_json",
    "resolve_claude_bin",
    "DEFAULT_PERMISSION_MODE",
    # session manager (U3)
    "SessionManager",
    "AgentPredicate",
    # fleet state + poller (U4)
    "FleetState",
    "FleetSnapshot",
    "ManagerSnapshot",
    "FleetPoller",
    "Subscriber",
    "tail_logs",
    "ATTENTION_STATES",
    "DEFAULT_LOG_LINES",
    "DEFAULT_POLL_INTERVAL",
    # status convention (U5)
    "ManagerStatus",
    "write_status",
    "read_status",
    "read_status_for",
    "read_statuses_for_agents",
    "status_path_for",
    "default_status_dir",
    "parse_sentinel",
    "STATUS_FILENAME",
    "STATUS_DIR_ENV",
    "ATTENTION_SENTINELS",
    "SENTINEL_RUNNING",
    "SENTINEL_CONTINUE",
    "SENTINEL_HUMAN_GATE",
    "SENTINEL_COMPLETE",
    "SENTINEL_FAILED",
]
