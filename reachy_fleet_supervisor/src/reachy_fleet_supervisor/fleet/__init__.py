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
from .body import (
    SIGNAL_DONE,
    SIGNAL_IDLE,
    ANTENNA_POSES,
    SIGNAL_FAILED,
    SIGNAL_WORKING,
    ANTENNA_MAX_DEG,
    SIGNAL_ATTENTION,
    BODY_YAW_SPAN_DEG,
    BodyPose,
    BodyApplier,
    FleetBodyRenderer,
    slot_yaw,
    antenna_pose,
    signal_state,
    choose_target,
    body_signal_for,
    make_goto_applier,
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
    strip_ansi,
    sanitize_lines,
)
from .config import (
    RunMode,
    GateMode,
    GatePolicy,
    GateDecision,
    GATE_ESCALATE,
    GATE_AUTONOMOUS,
    GATE_TRIGGER_KEY,
    FleetConfig,
    ProjectConfig,
    McpServerConfig,
    ProjectDefaults,
    FleetConfigError,
    classify_status,
    gate_trigger_of,
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
    status_from_transcript,
    read_statuses_for_agents,
)
from .drive import (
    DEFAULT_DRIVE_COMMAND,
    DEFAULT_SENTINEL_PREFIX,
    DriveLoopSpec,
    build_drive_task,
    spawn_drive_manager,
)
from .manager import (
    RUN_MODE_ENV,
    VALID_RUN_MODES,
    DEFAULT_RUN_MODE,
    PERMISSION_MODE_ENV,
    VALID_RC_SPAWN_MODES,
    DEFAULT_RC_SPAWN_MODE,
    VALID_PERMISSION_MODES,
    DEFAULT_PERMISSION_MODE,
    AgentInfo,
    FleetManager,
    FleetManagerError,
    list_agents,
    short_id_for,
    build_spawn_argv,
    build_steer_argv,
    resolve_run_mode,
    parse_agents_json,
    validate_run_mode,
    parse_spawn_output,
    resolve_claude_bin,
    resolve_permission_mode,
    validate_permission_mode,
    build_remote_control_server_argv,
)
from .control import (
    CONTROL_ACTIONS,
    ControlResult,
    FleetController,
)
from .runtime import (
    FleetRuntime,
    get_fleet_runtime,
    set_fleet_runtime,
    build_fleet_runtime,
    mount_fleet_dashboard,
)
from .identity import (
    PALETTE,
    AgentColor,
    color_for,
    assign_colors,
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
from .voice import (
    VOICE_GATE,
    VOICE_FAILED,
    VOICE_COMPLETED,
    SpeakFn,
    VoiceKind,
    VoiceAnnouncement,
    FleetVoiceRenderer,
    speakable_kind,
    voice_phrase_for,
)


__all__ = [
    # config (U1)
    "RunMode",
    "GateMode",
    "GatePolicy",
    "GateDecision",
    "GATE_ESCALATE",
    "GATE_AUTONOMOUS",
    "GATE_TRIGGER_KEY",
    "classify_status",
    "gate_trigger_of",
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
    "build_steer_argv",
    "build_remote_control_server_argv",
    "parse_spawn_output",
    "parse_agents_json",
    "resolve_claude_bin",
    "resolve_run_mode",
    "validate_run_mode",
    "resolve_permission_mode",
    "validate_permission_mode",
    "DEFAULT_PERMISSION_MODE",
    "PERMISSION_MODE_ENV",
    "VALID_PERMISSION_MODES",
    "DEFAULT_RUN_MODE",
    "RUN_MODE_ENV",
    "VALID_RUN_MODES",
    "DEFAULT_RC_SPAWN_MODE",
    "VALID_RC_SPAWN_MODES",
    # session manager (U3)
    "SessionManager",
    "AgentPredicate",
    # interactive controls (U12)
    "FleetController",
    "ControlResult",
    "CONTROL_ACTIONS",
    # process-wide runtime + app wiring (U10)
    "FleetRuntime",
    "build_fleet_runtime",
    "get_fleet_runtime",
    "set_fleet_runtime",
    "mount_fleet_dashboard",
    # fleet state + poller (U4)
    "FleetState",
    "FleetSnapshot",
    "ManagerSnapshot",
    "FleetPoller",
    "Subscriber",
    "tail_logs",
    "strip_ansi",
    "sanitize_lines",
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
    "status_from_transcript",
    # drive loops (U13)
    "DriveLoopSpec",
    "build_drive_task",
    "spawn_drive_manager",
    "DEFAULT_DRIVE_COMMAND",
    "DEFAULT_SENTINEL_PREFIX",
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
    # body renderer (U8)
    "BodyPose",
    "BodyApplier",
    "FleetBodyRenderer",
    "body_signal_for",
    "signal_state",
    "choose_target",
    "antenna_pose",
    "slot_yaw",
    "make_goto_applier",
    "ANTENNA_POSES",
    "ANTENNA_MAX_DEG",
    "BODY_YAW_SPAN_DEG",
    "SIGNAL_IDLE",
    "SIGNAL_WORKING",
    "SIGNAL_ATTENTION",
    "SIGNAL_DONE",
    "SIGNAL_FAILED",
    # voice renderer (U15)
    "FleetVoiceRenderer",
    "VoiceAnnouncement",
    "speakable_kind",
    "voice_phrase_for",
    "VoiceKind",
    "SpeakFn",
    "VOICE_FAILED",
    "VOICE_GATE",
    "VOICE_COMPLETED",
]
