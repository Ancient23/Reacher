"""Typed fleet configuration schema + loader.

The fleet config is a small declarative file (TOML or JSON) that lists the
projects a manager session can be pointed at and the default gate policy for
the whole fleet. Tasks themselves are given by voice at runtime (decision #11);
this file only describes *where* work can happen and *how autonomous* it is by
default (decision #13).

Schema (per plan.md §4 Phase 2):

    [fleet]
    # default gate policy applied to every project unless overridden
    [fleet.default_gate_policy]
    mode = "autonomous"          # or "gated"
    escalate_on = ["push"]       # gate triggers always escalated to the human

    [[fleet.projects]]
    name = "reacher"
    path = "C:/Source/reacher"
    env = { FOO = "bar" }
    defaults = { run_mode = "background", model = "claude-sonnet-4-6" }

      [[fleet.projects.mcp]]
      name = "unreal"
      command = "uv"
      args = ["run", "unreal-mcp"]

Everything is validated up front (pydantic v2, ``extra="forbid"``) so a typo in
the config fails loudly at load time rather than mid-spawn.
"""

from __future__ import annotations
import json
from typing import Literal
from pathlib import Path

import tomllib
from pydantic import Field, BaseModel, ConfigDict, field_validator, model_validator


# Per-manager run mode (decision #19): how the session runs, independent of what
# work pattern it executes. ``background`` = ``claude --bg`` (durable, the
# default); ``remote-control`` = a claude.ai/mobile-steerable session.
RunMode = Literal["background", "remote-control"]

# Gate policy mode (decision #13): autonomous by default; ``gated`` means every
# gate trigger pauses for the human, not just the ones in ``escalate_on``.
GateMode = Literal["autonomous", "gated"]


class FleetConfigError(ValueError):
    """Raised when a fleet config file is missing, malformed, or invalid."""


class _StrictModel(BaseModel):
    """Base model: reject unknown keys so typos fail loudly."""

    model_config = ConfigDict(extra="forbid")


class GatePolicy(_StrictModel):
    """How autonomous a worker is and which actions always escalate.

    ``mode="autonomous"`` runs the worker without interruption except for the
    triggers listed in ``escalate_on`` (e.g. ``"push"``, ``"publish"``).
    ``mode="gated"`` escalates on every gateable action regardless of the list.
    Concrete classification (autonomous vs escalate) lands in U14; U1 only fixes
    the shape and defaults.
    """

    mode: GateMode = "autonomous"
    escalate_on: list[str] = Field(default_factory=list)

    @field_validator("escalate_on")
    @classmethod
    def _dedupe_triggers(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        # Preserve order, drop duplicates.
        seen: set[str] = set()
        result: list[str] = []
        for item in cleaned:
            if item not in seen:
                seen.add(item)
                result.append(item)
        return result


class McpServerConfig(_StrictModel):
    """One MCP server made available to a worker (decision: MCP-general).

    Either a stdio server (``command`` + optional ``args``) or an HTTP/SSE
    server (``url``). Exactly one of the two must be provided. ``env`` is merged
    into the server's process environment at spawn time (U18 wires this through
    ``--mcp-config``).
    """

    name: str
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("mcp server name must not be blank")
        return value.strip()

    @model_validator(mode="after")
    def _require_command_or_url(self) -> McpServerConfig:
        has_command = bool(self.command and self.command.strip())
        has_url = bool(self.url and self.url.strip())
        if has_command == has_url:
            raise ValueError(
                f"mcp server '{self.name}' must set exactly one of 'command' (stdio) "
                "or 'url' (http/sse)"
            )
        return self


class ProjectDefaults(_StrictModel):
    """Per-project default spawn settings, overridable by voice at runtime.

    A project may override the fleet-wide gate policy via ``gate_policy``; when
    unset the fleet default applies (resolved by ``FleetConfig.gate_policy_for``).
    """

    run_mode: RunMode = "background"
    model: str | None = None
    permission_mode: str | None = None
    gate_policy: GatePolicy | None = None


class ProjectConfig(_StrictModel):
    """A project a manager can be pointed at.

    ``path`` is expanded (``~``) and recorded absolute, but is *not* required to
    exist at load time (a manager may create or clone it). ``name`` is the
    stable handle used by voice ("how's the unreal one doing?").
    """

    name: str
    path: Path
    env: dict[str, str] = Field(default_factory=dict)
    mcp: list[McpServerConfig] = Field(default_factory=list)
    defaults: ProjectDefaults = Field(default_factory=ProjectDefaults)

    @field_validator("name")
    @classmethod
    def _name_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("project name must not be blank")
        return value.strip()

    @field_validator("path")
    @classmethod
    def _expand_path(cls, value: Path) -> Path:
        return Path(value).expanduser()

    @model_validator(mode="after")
    def _unique_mcp_names(self) -> ProjectConfig:
        names = [server.name for server in self.mcp]
        dupes = sorted({name for name in names if names.count(name) > 1})
        if dupes:
            raise ValueError(
                f"project '{self.name}' has duplicate mcp server names: {dupes}"
            )
        return self


class FleetConfig(_StrictModel):
    """The whole fleet config: projects + a default gate policy."""

    projects: list[ProjectConfig] = Field(default_factory=list)
    default_gate_policy: GatePolicy = Field(default_factory=GatePolicy)

    @model_validator(mode="after")
    def _unique_project_names(self) -> FleetConfig:
        names = [project.name for project in self.projects]
        dupes = sorted({name for name in names if names.count(name) > 1})
        if dupes:
            raise ValueError(f"duplicate project names: {dupes}")
        return self

    def project(self, name: str) -> ProjectConfig:
        """Return the project with *name* or raise ``KeyError``."""
        for project in self.projects:
            if project.name == name:
                return project
        available = sorted(p.name for p in self.projects)
        raise KeyError(f"no project named {name!r}; available: {available}")

    def gate_policy_for(self, name: str) -> GatePolicy:
        """Effective gate policy for a project: its override or the fleet default."""
        override = self.project(name).defaults.gate_policy
        return override if override is not None else self.default_gate_policy


def load_fleet_config(path: str | Path) -> FleetConfig:
    """Load and validate a fleet config from a ``.toml`` or ``.json`` file.

    Raises :class:`FleetConfigError` for a missing file, bad syntax, an
    unsupported extension, or any schema validation failure.
    """
    config_path = Path(path).expanduser()
    if not config_path.is_file():
        raise FleetConfigError(f"fleet config not found: {config_path}")

    suffix = config_path.suffix.lower()
    raw = config_path.read_text(encoding="utf-8")
    try:
        if suffix == ".toml":
            data = tomllib.loads(raw)
        elif suffix == ".json":
            data = json.loads(raw)
        else:
            raise FleetConfigError(
                f"unsupported fleet config extension {suffix!r} ({config_path}); "
                "use .toml or .json"
            )
    except FleetConfigError:
        raise
    except (tomllib.TOMLDecodeError, json.JSONDecodeError) as exc:
        raise FleetConfigError(f"could not parse {config_path}: {exc}") from exc

    return parse_fleet_config(data)


def parse_fleet_config(data: object) -> FleetConfig:
    """Validate an already-parsed mapping into a :class:`FleetConfig`.

    Accepts either the bare fleet mapping (``{"projects": [...]}``) or a wrapped
    one (``{"fleet": {...}}``) so the same data can live in a shared config file.
    """
    if not isinstance(data, dict):
        raise FleetConfigError("fleet config must be a mapping at the top level")

    payload = data["fleet"] if "fleet" in data and isinstance(data["fleet"], dict) else data
    try:
        return FleetConfig.model_validate(payload)
    except ValueError as exc:  # pydantic ValidationError is a ValueError
        raise FleetConfigError(f"invalid fleet config: {exc}") from exc
