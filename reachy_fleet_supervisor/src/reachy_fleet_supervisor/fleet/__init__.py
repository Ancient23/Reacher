"""Fleet supervisor core (Phase 2+).

Holds the project-agnostic fleet model that the SessionManager, FleetState,
CLI, dashboard and robot body all build on. U1 adds the fleet *config* layer:
a typed, validated description of the projects a manager can be pointed at plus
the default gate policy. See plan.md §4 (Phase 2) and decisions #11, #13, #19.
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


__all__ = [
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
]
