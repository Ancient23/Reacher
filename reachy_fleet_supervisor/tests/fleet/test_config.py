"""Tests for the fleet config schema + loader (U1)."""

from __future__ import annotations
import json
from pathlib import Path

import pytest

from reachy_fleet_supervisor.fleet import (
    GatePolicy,
    FleetConfig,
    ProjectConfig,
    ManagerStatus,
    GATE_ESCALATE,
    McpServerConfig,
    GATE_AUTONOMOUS,
    FleetConfigError,
    classify_status,
    gate_trigger_of,
    load_fleet_config,
    mcp_server_entry,
    parse_fleet_config,
    mcp_config_payload,
    SENTINEL_FAILED,
    write_mcp_config_file,
    SENTINEL_HUMAN_GATE,
    SENTINEL_COMPLETE,
)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def test_empty_fleet_has_sensible_defaults() -> None:
    """An empty fleet has no projects and an autonomous default policy."""
    cfg = FleetConfig()
    assert cfg.projects == []
    assert cfg.default_gate_policy.mode == "autonomous"
    assert cfg.default_gate_policy.escalate_on == []


def test_project_defaults() -> None:
    """A bare project leaves run_mode/model/permission_mode unset (inherit fleet)."""
    project = ProjectConfig(name="p", path="/tmp/p")
    assert project.defaults.run_mode is None  # unset → inherit fleet default
    assert project.defaults.model is None
    assert project.defaults.permission_mode is None
    assert project.defaults.gate_policy is None
    assert project.env == {}
    assert project.mcp == []


# ---------------------------------------------------------------------------
# Permission mode (U10 deadlock fix): fleet default + per-project override
# ---------------------------------------------------------------------------


def test_fleet_default_permission_mode_is_non_interactive() -> None:
    """Default permission mode is bypassPermissions so managers don't deadlock."""
    assert FleetConfig().permission_mode == "bypassPermissions"


def test_permission_mode_for_uses_fleet_default_when_project_unset() -> None:
    cfg = parse_fleet_config({"projects": [{"name": "a", "path": "/x"}]})
    assert cfg.permission_mode_for("a") == "bypassPermissions"


def test_permission_mode_for_project_override_wins() -> None:
    """A project's permission_mode overrides the fleet-wide default."""
    cfg = parse_fleet_config(
        {
            "permission_mode": "bypassPermissions",
            "projects": [
                {"name": "a", "path": "/x", "defaults": {"permission_mode": "acceptEdits"}},
                {"name": "b", "path": "/y"},
            ],
        }
    )
    assert cfg.permission_mode_for("a") == "acceptEdits"  # override
    assert cfg.permission_mode_for("b") == "bypassPermissions"  # fleet default


def test_custom_fleet_default_permission_mode_honored() -> None:
    cfg = parse_fleet_config({"permission_mode": "acceptEdits", "projects": []})
    assert cfg.permission_mode == "acceptEdits"


def test_invalid_fleet_permission_mode_rejected() -> None:
    with pytest.raises(FleetConfigError):
        parse_fleet_config({"permission_mode": "yolo", "projects": []})


def test_invalid_project_permission_mode_rejected() -> None:
    with pytest.raises(FleetConfigError):
        parse_fleet_config(
            {"projects": [{"name": "a", "path": "/x", "defaults": {"permission_mode": "nope"}}]}
        )


# ---------------------------------------------------------------------------
# run_mode (U11) — fleet-wide default + per-project resolution
# ---------------------------------------------------------------------------


def test_fleet_default_run_mode_is_background() -> None:
    assert FleetConfig().run_mode == "background"


def test_custom_fleet_default_run_mode_honored() -> None:
    cfg = parse_fleet_config({"run_mode": "remote-control", "projects": []})
    assert cfg.run_mode == "remote-control"


def test_invalid_fleet_run_mode_rejected() -> None:
    with pytest.raises(FleetConfigError):
        parse_fleet_config({"run_mode": "server", "projects": []})


def test_run_mode_for_uses_fleet_default_when_project_unset() -> None:
    """A project that leaves run_mode at the shared default inherits the fleet default."""
    cfg = parse_fleet_config(
        {"run_mode": "remote-control", "projects": [{"name": "a", "path": "/x"}]}
    )
    assert cfg.run_mode_for("a") == "remote-control"


def test_run_mode_for_project_override_wins() -> None:
    """A project's explicit run_mode overrides the fleet-wide default."""
    cfg = parse_fleet_config(
        {
            "run_mode": "remote-control",
            "projects": [
                {"name": "a", "path": "/x", "defaults": {"run_mode": "background"}},
                {"name": "b", "path": "/y"},
            ],
        }
    )
    assert cfg.run_mode_for("a") == "background"  # explicit project override
    assert cfg.run_mode_for("b") == "remote-control"  # fleet default


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_unknown_key_rejected() -> None:
    """Unknown top-level keys are rejected (extra=forbid)."""
    with pytest.raises(FleetConfigError):
        parse_fleet_config({"projects": [], "bogus": 1})


def test_duplicate_project_names_rejected() -> None:
    """Two projects with the same name fail validation."""
    with pytest.raises(FleetConfigError) as exc:
        parse_fleet_config(
            {"projects": [{"name": "a", "path": "/x"}, {"name": "a", "path": "/y"}]}
        )
    assert "duplicate project names" in str(exc.value)


def test_blank_project_name_rejected() -> None:
    """A whitespace-only project name is rejected."""
    with pytest.raises(FleetConfigError):
        parse_fleet_config({"projects": [{"name": "  ", "path": "/x"}]})


def test_project_name_stripped() -> None:
    """Project names are trimmed of surrounding whitespace."""
    cfg = parse_fleet_config({"projects": [{"name": "  hi  ", "path": "/x"}]})
    assert cfg.projects[0].name == "hi"


def test_path_expanded_user() -> None:
    """A leading ~ in a project path is expanded."""
    cfg = parse_fleet_config({"projects": [{"name": "p", "path": "~/proj"}]})
    assert "~" not in str(cfg.projects[0].path)
    assert cfg.projects[0].path == Path("~/proj").expanduser()


def test_invalid_run_mode_rejected() -> None:
    """An unknown run_mode value is rejected."""
    with pytest.raises(FleetConfigError):
        parse_fleet_config(
            {"projects": [{"name": "p", "path": "/x", "defaults": {"run_mode": "nope"}}]}
        )


def test_invalid_gate_mode_rejected() -> None:
    """An unknown gate policy mode is rejected."""
    with pytest.raises(FleetConfigError):
        parse_fleet_config({"default_gate_policy": {"mode": "loose"}})


def test_escalate_on_deduped_and_trimmed() -> None:
    """escalate_on triggers are trimmed and de-duplicated, order preserved."""
    gp = GatePolicy(escalate_on=["push", " push ", "publish", ""])
    assert gp.escalate_on == ["push", "publish"]


# ---------------------------------------------------------------------------
# MCP servers
# ---------------------------------------------------------------------------


def test_mcp_stdio_server_ok() -> None:
    """A stdio MCP server (command + args) validates."""
    server = McpServerConfig(name="unreal", command="uv", args=["run", "x"])
    assert server.command == "uv"
    assert server.url is None


def test_mcp_http_server_ok() -> None:
    """An HTTP MCP server (url) validates."""
    server = McpServerConfig(name="web", url="http://localhost:9000")
    assert server.url == "http://localhost:9000"


def test_mcp_requires_command_xor_url() -> None:
    """An MCP server must set exactly one of command or url."""
    with pytest.raises(ValueError):
        McpServerConfig(name="bad")  # neither
    with pytest.raises(ValueError):
        McpServerConfig(name="bad", command="x", url="http://y")  # both


def test_duplicate_mcp_names_rejected() -> None:
    """Two MCP servers with the same name in one project fail validation."""
    with pytest.raises(FleetConfigError):
        parse_fleet_config(
            {
                "projects": [
                    {
                        "name": "p",
                        "path": "/x",
                        "mcp": [
                            {"name": "m", "command": "a"},
                            {"name": "m", "command": "b"},
                        ],
                    }
                ]
            }
        )


# ---------------------------------------------------------------------------
# Lookups + effective gate policy
# ---------------------------------------------------------------------------


def test_project_lookup_and_missing() -> None:
    """project() returns a known project and raises KeyError otherwise."""
    cfg = parse_fleet_config({"projects": [{"name": "a", "path": "/x"}]})
    assert cfg.project("a").name == "a"
    with pytest.raises(KeyError):
        cfg.project("nope")


def test_gate_policy_falls_back_to_default() -> None:
    """A project without an override inherits the fleet default policy."""
    cfg = parse_fleet_config(
        {
            "default_gate_policy": {"mode": "autonomous", "escalate_on": ["push"]},
            "projects": [{"name": "a", "path": "/x"}],
        }
    )
    assert cfg.gate_policy_for("a").escalate_on == ["push"]
    assert cfg.gate_policy_for("a").mode == "autonomous"


def test_gate_policy_override_per_project() -> None:
    """A project gate_policy override wins over the fleet default."""
    cfg = parse_fleet_config(
        {
            "default_gate_policy": {"mode": "autonomous"},
            "projects": [
                {
                    "name": "a",
                    "path": "/x",
                    "defaults": {"gate_policy": {"mode": "gated"}},
                }
            ],
        }
    )
    assert cfg.gate_policy_for("a").mode == "gated"


# ---------------------------------------------------------------------------
# Loader (TOML / JSON / wrapped / errors)
# ---------------------------------------------------------------------------


def test_load_toml(tmp_path: Path) -> None:
    """A full TOML fleet config loads and round-trips nested fields."""
    p = tmp_path / "fleet.toml"
    p.write_text(
        """
[default_gate_policy]
mode = "autonomous"
escalate_on = ["push"]

[[projects]]
name = "reacher"
path = "/src/reacher"

  [projects.defaults]
  run_mode = "background"
  model = "claude-sonnet-4-6"

  [[projects.mcp]]
  name = "unreal"
  command = "uv"
  args = ["run", "unreal-mcp"]
""",
        encoding="utf-8",
    )
    cfg = load_fleet_config(p)
    assert cfg.project("reacher").defaults.model == "claude-sonnet-4-6"
    assert cfg.project("reacher").mcp[0].name == "unreal"
    assert cfg.default_gate_policy.escalate_on == ["push"]


def test_load_json(tmp_path: Path) -> None:
    """A JSON fleet config loads."""
    p = tmp_path / "fleet.json"
    p.write_text(
        json.dumps({"projects": [{"name": "a", "path": "/x"}]}),
        encoding="utf-8",
    )
    cfg = load_fleet_config(p)
    assert cfg.project("a").path == Path("/x")


def test_load_wrapped_fleet_key(tmp_path: Path) -> None:
    """A config wrapped under a top-level 'fleet' key is unwrapped."""
    p = tmp_path / "shared.json"
    p.write_text(
        json.dumps({"fleet": {"projects": [{"name": "a", "path": "/x"}]}, "other": 1}),
        encoding="utf-8",
    )
    cfg = load_fleet_config(p)
    assert cfg.project("a").name == "a"


def test_load_missing_file(tmp_path: Path) -> None:
    """Loading a non-existent file raises FleetConfigError."""
    with pytest.raises(FleetConfigError) as exc:
        load_fleet_config(tmp_path / "nope.toml")
    assert "not found" in str(exc.value)


def test_load_unsupported_extension(tmp_path: Path) -> None:
    """An unsupported file extension raises FleetConfigError."""
    p = tmp_path / "fleet.yaml"
    p.write_text("projects: []", encoding="utf-8")
    with pytest.raises(FleetConfigError) as exc:
        load_fleet_config(p)
    assert "unsupported" in str(exc.value)


def test_load_bad_toml(tmp_path: Path) -> None:
    """Malformed TOML raises FleetConfigError with a parse message."""
    p = tmp_path / "fleet.toml"
    p.write_text("this = = bad", encoding="utf-8")
    with pytest.raises(FleetConfigError) as exc:
        load_fleet_config(p)
    assert "could not parse" in str(exc.value)


def test_shipped_example_config_is_valid() -> None:
    """The example config shipped in the repo loads and validates."""
    example = Path(__file__).resolve().parents[2] / "fleet.example.toml"
    assert example.is_file(), f"missing example config at {example}"
    cfg = load_fleet_config(example)
    assert {p.name for p in cfg.projects} == {"reacher", "unreal"}
    assert cfg.gate_policy_for("unreal").mode == "gated"
    assert cfg.gate_policy_for("reacher").mode == "autonomous"

# ---------------------------------------------------------------------------
# Gate classification (U14, decision #13): autonomous vs escalate
# ---------------------------------------------------------------------------


def test_autonomous_policy_only_escalates_listed_triggers() -> None:
    """In autonomous mode only triggers in escalate_on escalate; others run on."""
    gp = GatePolicy(mode="autonomous", escalate_on=["push", "publish"])
    assert gp.classify("push") == GATE_ESCALATE
    assert gp.classify("PUBLISH") == GATE_ESCALATE  # case-insensitive
    assert gp.classify("edit") == GATE_AUTONOMOUS
    assert gp.escalates("push") is True
    assert gp.escalates("edit") is False


def test_gated_policy_escalates_every_trigger() -> None:
    """In gated mode every non-empty trigger escalates regardless of the list."""
    gp = GatePolicy(mode="gated")
    assert gp.classify("edit") == GATE_ESCALATE
    assert gp.classify("anything") == GATE_ESCALATE


def test_empty_trigger_never_escalates_on_its_own() -> None:
    """A blank/None trigger is not a gateable action."""
    assert GatePolicy(mode="gated").classify(None) == GATE_AUTONOMOUS
    assert GatePolicy(mode="gated").classify("   ") == GATE_AUTONOMOUS
    assert GatePolicy(mode="autonomous").escalates("") is False


def test_gate_trigger_of_reads_extra_label() -> None:
    """gate_trigger_of pulls the labelled trigger off the status, else None."""
    labelled = ManagerStatus(state=SENTINEL_HUMAN_GATE, extra={"gate_trigger": " push "})
    assert gate_trigger_of(labelled) == "push"
    assert gate_trigger_of(ManagerStatus(state=SENTINEL_HUMAN_GATE)) is None
    assert gate_trigger_of(ManagerStatus(state=SENTINEL_HUMAN_GATE, extra={"gate_trigger": ""})) is None


def test_classify_status_non_attention_is_autonomous() -> None:
    """RUNNING/CONTINUE/COMPLETE have nothing to gate."""
    gp = GatePolicy(mode="gated")  # strictest mode still autonomous here
    assert classify_status(gp, ManagerStatus(state="RUNNING")) == GATE_AUTONOMOUS
    assert classify_status(gp, ManagerStatus(state=SENTINEL_COMPLETE)) == GATE_AUTONOMOUS


def test_classify_status_failed_always_escalates() -> None:
    """A hard failure is never auto-approvable, even in autonomous mode."""
    gp = GatePolicy(mode="autonomous", escalate_on=[])
    assert classify_status(gp, ManagerStatus(state=SENTINEL_FAILED)) == GATE_ESCALATE


def test_classify_status_bare_human_gate_escalates() -> None:
    """An unlabelled HUMAN_GATE asks for a human regardless of mode."""
    gp = GatePolicy(mode="autonomous", escalate_on=[])
    assert classify_status(gp, ManagerStatus(state=SENTINEL_HUMAN_GATE)) == GATE_ESCALATE


def test_classify_status_labelled_gate_follows_policy() -> None:
    """A labelled HUMAN_GATE is classified by the trigger against the policy."""
    auto = GatePolicy(mode="autonomous", escalate_on=["push"])
    push_gate = ManagerStatus(state=SENTINEL_HUMAN_GATE, extra={"gate_trigger": "push"})
    docs_gate = ManagerStatus(state=SENTINEL_HUMAN_GATE, extra={"gate_trigger": "docs"})
    assert classify_status(auto, push_gate) == GATE_ESCALATE
    assert classify_status(auto, docs_gate) == GATE_AUTONOMOUS
    # gated mode escalates the docs gate too.
    assert classify_status(GatePolicy(mode="gated"), docs_gate) == GATE_ESCALATE


def test_gate_policy_for_explicit_override_wins() -> None:
    """Explicit per-worker override beats project override beats fleet default."""
    cfg = parse_fleet_config(
        {
            "default_gate_policy": {"mode": "autonomous"},
            "projects": [
                {"name": "a", "path": "/x", "defaults": {"gate_policy": {"mode": "gated"}}},
            ],
        }
    )
    explicit = GatePolicy(mode="autonomous", escalate_on=["push"])
    assert cfg.gate_policy_for("a").mode == "gated"  # project override
    assert cfg.gate_policy_for("a", override=explicit).escalate_on == ["push"]  # explicit wins


def test_classify_for_uses_effective_policy() -> None:
    """classify_for / classify_status_for resolve the project's effective policy."""
    cfg = parse_fleet_config(
        {
            "default_gate_policy": {"mode": "autonomous", "escalate_on": ["push"]},
            "projects": [
                {"name": "gated_proj", "path": "/x", "defaults": {"gate_policy": {"mode": "gated"}}},
                {"name": "auto_proj", "path": "/y"},
            ],
        }
    )
    # autonomous project: only "push" escalates.
    assert cfg.classify_for("auto_proj", "push") == GATE_ESCALATE
    assert cfg.classify_for("auto_proj", "edit") == GATE_AUTONOMOUS
    # gated project: everything escalates.
    assert cfg.classify_for("gated_proj", "edit") == GATE_ESCALATE
    # status-level helper.
    push_gate = ManagerStatus(state=SENTINEL_HUMAN_GATE, extra={"gate_trigger": "push"})
    assert cfg.classify_status_for("auto_proj", push_gate) == GATE_ESCALATE


# ---------------------------------------------------------------------------
# MCP-general per-worker MCP config (U18)
# ---------------------------------------------------------------------------


def test_mcp_server_entry_stdio() -> None:
    """A stdio server becomes {command, args, env} (fields omitted when empty)."""
    bare = McpServerConfig(name="unreal", command="uv")
    assert mcp_server_entry(bare) == {"command": "uv"}

    full = McpServerConfig(
        name="unreal", command="uv", args=["run", "unreal-mcp"], env={"UE_PORT": "9000"}
    )
    assert mcp_server_entry(full) == {
        "command": "uv",
        "args": ["run", "unreal-mcp"],
        "env": {"UE_PORT": "9000"},
    }


def test_mcp_server_entry_http() -> None:
    """A url server becomes {type: http, url, [env]}."""
    server = McpServerConfig(name="browser", url="http://localhost:9001/mcp")
    assert mcp_server_entry(server) == {"type": "http", "url": "http://localhost:9001/mcp"}


def test_mcp_config_payload_shape() -> None:
    """The whole payload is {mcpServers: {name: entry, ...}}."""
    servers = [
        McpServerConfig(name="unreal", command="uv", args=["run", "unreal-mcp"]),
        McpServerConfig(name="browser", url="http://localhost:9001/mcp"),
    ]
    payload = mcp_config_payload(servers)
    assert payload == {
        "mcpServers": {
            "unreal": {"command": "uv", "args": ["run", "unreal-mcp"]},
            "browser": {"type": "http", "url": "http://localhost:9001/mcp"},
        }
    }
    assert mcp_config_payload([]) == {"mcpServers": {}}


def test_write_mcp_config_file_empty_writes_nothing(tmp_path: Path) -> None:
    """No mcp servers -> no file written, return None (no hard dependency, U18)."""
    target = tmp_path / "sub" / "cfg.json"
    result = write_mcp_config_file([], target)
    assert result is None
    assert not target.exists()


def test_write_mcp_config_file_writes_json(tmp_path: Path) -> None:
    """Non-empty servers materialize a valid --mcp-config JSON file, dirs created."""
    target = tmp_path / "sub" / "cfg.json"
    servers = [McpServerConfig(name="unreal", command="uv", args=["run", "unreal-mcp"])]
    result = write_mcp_config_file(servers, target)
    assert result == target
    assert target.is_file()
    assert json.loads(target.read_text(encoding="utf-8")) == {
        "mcpServers": {"unreal": {"command": "uv", "args": ["run", "unreal-mcp"]}}
    }


def test_project_materialize_mcp_config(tmp_path: Path) -> None:
    """ProjectConfig.materialize_mcp_config wraps write_mcp_config_file for its own list."""
    empty_project = ProjectConfig(name="empty", path="/x")
    assert empty_project.materialize_mcp_config(tmp_path / "empty.json") is None

    project = ProjectConfig(
        name="unreal_proj",
        path="/x",
        mcp=[McpServerConfig(name="unreal", command="uv", args=["run", "unreal-mcp"])],
    )
    out = project.materialize_mcp_config(tmp_path / "unreal_proj.mcp.json")
    assert out is not None and out.is_file()
    assert json.loads(out.read_text(encoding="utf-8"))["mcpServers"]["unreal"]["command"] == "uv"
