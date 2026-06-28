"""Tests for the fleet config schema + loader (U1)."""

from __future__ import annotations
import json
from pathlib import Path

import pytest

from reachy_fleet_supervisor.fleet import (
    GatePolicy,
    FleetConfig,
    ProjectConfig,
    McpServerConfig,
    FleetConfigError,
    load_fleet_config,
    parse_fleet_config,
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
    """A bare project defaults to background run mode and empty collections."""
    project = ProjectConfig(name="p", path="/tmp/p")
    assert project.defaults.run_mode == "background"
    assert project.defaults.model is None
    assert project.defaults.gate_policy is None
    assert project.env == {}
    assert project.mcp == []


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
