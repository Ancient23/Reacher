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
- U6 adds the headless ``fleet`` CLI (:mod:`.cli`): ``fleet
  start|spawn|status|logs|stop`` over SessionManager + FleetState — the
  scriptable dev/test harness, also the ``fleet`` console entry point.
- U7 adds the read-only web dashboard (:mod:`.dashboard`,
  :func:`create_dashboard_app`): a FastAPI 2–4 card grid + JSON poll endpoint
  over FleetState, mountable on the Reachy Mini settings app.
- U9 adds per-agent *identity* (:mod:`.identity`, :class:`AgentColor`,
  :func:`assign_colors`): a stable name + distinct color per manager, surfaced
  through FleetState / CLI / dashboard so 2–4 concurrent managers are
  distinguishable at a glance and "the amber one" means the same one everywhere.
"""

from __future__ import annotations

from .cli import (
    main as cli_main,
)
from .cli import (
    build_parser,
)
from .state import (
    ATTENTION_STATES,
    DEFAULT_LOG_LINES,
    DEFAULT_POLL_INTERVAL,
    FleetState,
    Subscriber,
    FleetPoller,
    FleetSnapshot,
    ManagerSnapshot,
    tail_logs,
)
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
from .status import (
    STATUS_DIR_ENV,
    SENTINEL_FAILED,
    STATUS_FILENAME,
    SENTINEL_RUNNING,
    SENTINEL_COMPLETE,
    SENTINEL_CONTINUE,
    ATTENTION_SENTINELS,
    SENTINEL_HUMAN_GATE,
    ManagerStatus,
    read_status,
    write_status,
    parse_sentinel,
    read_status_for,
    status_path_for,
    default_status_dir,
    read_statuses_for_agents,
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
from .dashboard import (
    DEFAULT_POLL_MS,
    render_page,
    snapshot_payload,
    create_dashboard_app,
)
from .session_manager import (
    AgentPredicate,
    SessionManager,
)
from .identity import (
    PALETTE,
    AgentColor,
    color_for,
    assign_colors,
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
    # headless CLI (U6)
    "build_parser",
    "cli_main",
    # web dashboard (U7)
    "create_dashboard_app",
    "render_page",
    "snapshot_payload",
    "DEFAULT_POLL_MS",
    # per-agent identity (U9)
    "AgentColor",
    "PALETTE",
    "color_for",
    "assign_colors",
]
