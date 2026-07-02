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
from typing import Literal, Optional, Sequence
from pathlib import Path

import tomllib
from pydantic import Field, BaseModel, ConfigDict, field_validator, model_validator

from .manager import (
    VALID_RUN_MODES,
    DEFAULT_RUN_MODE,
    VALID_PERMISSION_MODES,
    DEFAULT_PERMISSION_MODE,
    RunMode,
)
from .status import (
    SENTINEL_FAILED,
    SENTINEL_HUMAN_GATE,
    ManagerStatus,
)


# ``RunMode`` (per-manager hosting: ``background`` | ``remote-control``, decision
# #19) is defined in :mod:`.manager` — the single source of truth shared by the
# spawn path and this config — and re-exported here (used by ``ProjectDefaults``
# and ``FleetConfig`` below) for config consumers.

# Gate policy mode (decision #13): autonomous by default; ``gated`` means every
# gate trigger pauses for the human, not just the ones in ``escalate_on``.
GateMode = Literal["autonomous", "gated"]

# Classification of a gate signal (decision #13): the loop either keeps running
# without a human (``autonomous``) or pauses for one (``escalate`` — U15 speaks it).
GateDecision = Literal["autonomous", "escalate"]
GATE_AUTONOMOUS: GateDecision = "autonomous"
GATE_ESCALATE: GateDecision = "escalate"

# Key under ``ManagerStatus.extra`` a manager may set to label *why* it gated
# (e.g. ``"push"``, ``"publish"``, ``"hardware"``) so the policy can classify it.
GATE_TRIGGER_KEY = "gate_trigger"


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

    def escalates(self, trigger: str | None) -> bool:
        """Whether a gateable action labelled *trigger* must escalate to a human.

        Decision #13 classification: in ``gated`` mode every non-empty trigger
        escalates; in ``autonomous`` mode only triggers listed in ``escalate_on``
        do (matched case-insensitively). An empty/``None`` trigger is *not* a
        gateable action and never escalates on its own.
        """
        norm = (trigger or "").strip().lower()
        if not norm:
            return False
        if self.mode == "gated":
            return True
        return norm in {item.lower() for item in self.escalate_on}

    def classify(self, trigger: str | None) -> GateDecision:
        """Classify a gate *trigger* as :data:`GATE_ESCALATE` or :data:`GATE_AUTONOMOUS`."""
        return GATE_ESCALATE if self.escalates(trigger) else GATE_AUTONOMOUS


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
    Likewise ``permission_mode`` overrides the fleet-wide default
    (``FleetConfig.permission_mode``, resolved by ``permission_mode_for``) and
    ``run_mode`` overrides the fleet-wide default (``FleetConfig.run_mode``,
    resolved by ``run_mode_for``); when either is unset (``None``) the fleet
    default applies. A spawned manager has no TTY, so a permission mode that still
    prompts deadlocks it — see ``manager.DEFAULT_PERMISSION_MODE``.
    """

    run_mode: RunMode | None = None
    model: str | None = None
    permission_mode: str | None = None
    gate_policy: GatePolicy | None = None

    @field_validator("permission_mode")
    @classmethod
    def _valid_permission_mode(cls, value: str | None) -> str | None:
        if value is not None and value not in VALID_PERMISSION_MODES:
            raise ValueError(
                f"invalid permission_mode {value!r}; valid: {sorted(VALID_PERMISSION_MODES)}"
            )
        return value


def mcp_server_entry(server: McpServerConfig) -> dict:
    """Build one entry of a Claude Code ``--mcp-config`` ``mcpServers`` map.

    Stdio servers (``command`` set) -> ``{"command": ..., "args": [...], "env":
    {...}}`` (``args``/``env`` omitted when empty, matching the CLI's own
    minimal shape). HTTP/SSE servers (``url`` set) -> ``{"type": "http", "url":
    ...}``. ``McpServerConfig`` already guarantees exactly one of the two is set.
    """
    if server.command:
        entry: dict[str, object] = {"command": server.command}
        if server.args:
            entry["args"] = list(server.args)
        if server.env:
            entry["env"] = dict(server.env)
        return entry
    entry = {"type": "http", "url": server.url}
    if server.env:
        entry["env"] = dict(server.env)
    return entry


def mcp_config_payload(servers: Sequence[McpServerConfig]) -> dict:
    """Build the whole ``--mcp-config`` JSON payload for a list of servers.

    Shape: ``{"mcpServers": {"<name>": {...}, ...}}`` — the format Claude Code's
    ``--mcp-config <file>`` flag expects. Returns ``{"mcpServers": {}}`` for an
    empty list (callers typically skip writing a file in that case — see
    :func:`write_mcp_config_file`).
    """
    return {"mcpServers": {server.name: mcp_server_entry(server) for server in servers}}


def write_mcp_config_file(servers: Sequence[McpServerConfig], path: str | Path) -> Optional[Path]:
    """Materialize *servers* as a ``--mcp-config`` JSON file at *path*.

    Returns ``None`` (writing nothing) when *servers* is empty — a project with
    no ``mcp`` entries needs no ``--mcp-config`` flag at all. Otherwise writes
    the file (creating parent directories as needed) and returns its path, so
    the caller can pass it straight into ``mcp_configs=[...]`` at spawn time
    (U2's ``build_spawn_argv`` / ``FleetManager.spawn``).
    """
    if not servers:
        return None
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(mcp_config_payload(servers), indent=2), encoding="utf-8")
    return target


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

    def materialize_mcp_config(self, path: str | Path) -> Optional[Path]:
        """Write this project's ``mcp`` servers to *path* as a ``--mcp-config`` file.

        Thin wrapper around :func:`write_mcp_config_file` for the project's own
        ``mcp`` list. Returns ``None`` (writes nothing) when the project has no
        MCP servers configured — U18's "config only, no hard dependency."
        """
        return write_mcp_config_file(self.mcp, path)


class FleetConfig(_StrictModel):
    """The whole fleet config: projects + a default gate policy.

    ``permission_mode`` is the fleet-wide default ``--permission-mode`` for
    spawned managers (overridable per project via ``ProjectDefaults`` and per
    spawn at the call site / voice tool). It defaults to
    ``manager.DEFAULT_PERMISSION_MODE`` (``bypassPermissions``) so a trusted local
    manager runs non-interactively and does not deadlock on a permission prompt
    it has no TTY to answer. SECURITY: ``bypassPermissions`` skips all permission
    checks for that session — change this if you want managers gated.
    """

    projects: list[ProjectConfig] = Field(default_factory=list)
    default_gate_policy: GatePolicy = Field(default_factory=GatePolicy)
    permission_mode: str = DEFAULT_PERMISSION_MODE
    run_mode: RunMode = DEFAULT_RUN_MODE

    @field_validator("permission_mode")
    @classmethod
    def _valid_permission_mode(cls, value: str) -> str:
        if value not in VALID_PERMISSION_MODES:
            raise ValueError(
                f"invalid permission_mode {value!r}; valid: {sorted(VALID_PERMISSION_MODES)}"
            )
        return value

    @field_validator("run_mode")
    @classmethod
    def _valid_run_mode(cls, value: str) -> str:
        if value not in VALID_RUN_MODES:
            raise ValueError(
                f"invalid run_mode {value!r}; valid: {sorted(VALID_RUN_MODES)}"
            )
        return value

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

    def gate_policy_for(
        self, name: str, *, override: GatePolicy | None = None
    ) -> GatePolicy:
        """Effective gate policy for a project.

        Precedence (highest first), mirroring ``run_mode``/``permission_mode``:
        an explicit per-worker *override* (set by voice / the spawn call site),
        then the project's ``defaults.gate_policy``, then the fleet
        ``default_gate_policy``.
        """
        if override is not None:
            return override
        project_override = self.project(name).defaults.gate_policy
        return project_override if project_override is not None else self.default_gate_policy

    def classify_for(
        self, name: str, trigger: str | None, *, override: GatePolicy | None = None
    ) -> GateDecision:
        """Classify a gate *trigger* under project *name*'s effective policy."""
        return self.gate_policy_for(name, override=override).classify(trigger)

    def classify_status_for(
        self,
        name: str,
        status: ManagerStatus,
        *,
        override: GatePolicy | None = None,
    ) -> GateDecision:
        """Classify a manager's emitted *status* under project *name*'s policy."""
        return classify_status(self.gate_policy_for(name, override=override), status)

    def permission_mode_for(self, name: str) -> str:
        """Effective ``--permission-mode`` for a project: override or fleet default."""
        override = self.project(name).defaults.permission_mode
        return override if override is not None else self.permission_mode

    def run_mode_for(self, name: str) -> str:
        """Effective ``run_mode`` for a project: its override or the fleet default.

        A voice request still overrides both at spawn time (``resolve_run_mode``).
        """
        override = self.project(name).defaults.run_mode
        return override if override is not None else self.run_mode


def gate_trigger_of(status: ManagerStatus) -> str | None:
    """Extract the gate *trigger* label a manager recorded, if any.

    Looks under ``status.extra[GATE_TRIGGER_KEY]`` — the optional keyword a drive
    loop sets to say *why* it raised a gate (e.g. ``"push"``). Returns a trimmed
    string or ``None`` when absent/blank.
    """
    raw = status.extra.get(GATE_TRIGGER_KEY)
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    return cleaned or None


def classify_status(policy: GatePolicy, status: ManagerStatus) -> GateDecision:
    """Classify a manager's emitted *status* against a gate *policy* (decision #13).

    This plugs the real gate signal (the sentinel a manager emits to its
    ``status.json`` / prints to its transcript — :class:`ManagerStatus`) into the
    policy:

    - ``FAILED`` always escalates — a hard failure is not auto-approvable.
    - ``HUMAN_GATE`` is the gate decision point: if the manager labelled it with a
      trigger (``extra["gate_trigger"]``), the policy decides escalate vs continue;
      a *bare* gate (no label) always escalates, since the manager explicitly asked
      for a human. ``gated`` mode escalates every gate regardless.
    - Any other state (``RUNNING``/``CONTINUE``/``COMPLETE``) is autonomous — there
      is nothing to gate.
    """
    state = status.normalized_state
    if state == SENTINEL_FAILED:
        return GATE_ESCALATE
    if state == SENTINEL_HUMAN_GATE:
        trigger = gate_trigger_of(status)
        if trigger is None:
            # A bare gate (no label to match against ``escalate_on``) is the worker
            # explicitly asking for a human — escalate regardless of mode.
            return GATE_ESCALATE
        return policy.classify(trigger)
    return GATE_AUTONOMOUS


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
