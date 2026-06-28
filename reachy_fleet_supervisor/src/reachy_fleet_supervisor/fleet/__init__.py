"""Fleet supervisor core (Phase 2+).

Holds the project-agnostic fleet model that the SessionManager, FleetState,
CLI, dashboard and robot body all build on.

- U1 adds the fleet *config* layer: a typed, validated description of the
  projects a manager can be pointed at plus the default gate policy.
- U2 adds :class:`FleetManager`: spawn + control of one ``claude --bg``
  background manager session (the fleet's durable substrate). See plan.md §4
  (Phase 2) and decisions #11, #13, #19.
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
]
