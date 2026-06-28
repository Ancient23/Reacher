"""Tests for the headless ``fleet`` CLI (U6).

Logic tests drive ``cli.main`` with a mocked ``claude agents --json`` roster and
mocked control calls, asserting both the human-readable and ``--json`` output of
``start`` / ``spawn`` / ``status`` / ``logs`` / ``stop``. One integration test
drives the CLI end-to-end against a REAL trivial background session (spawn →
status → logs → stop --rm) and ALWAYS tears it down; it is skipped when the
``claude`` CLI is not installed.
"""

from __future__ import annotations
import io
import json
import uuid
import shutil

import pytest

from reachy_fleet_supervisor.fleet import cli
from reachy_fleet_supervisor.fleet import state as state_mod
from reachy_fleet_supervisor.fleet import session_manager as sm_mod
from reachy_fleet_supervisor.fleet import (
    AgentInfo,
    FleetManager,
    ManagerStatus,
    write_status,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bg_row(short_id: str, name: str, **extra: object) -> dict[str, object]:
    row = {
        "id": short_id,
        "sessionId": f"{short_id}-1111-2222-3333-444444444444",
        "kind": "background",
        "name": name,
        "cwd": "/proj",
        "state": "working",
    }
    row.update(extra)
    return row


def _run(argv, *, out=None, err=None):
    """Run cli.main capturing stdout/stderr; return (code, out_text, err_text)."""
    out = out or io.StringIO()
    err = err or io.StringIO()
    code = cli.main(argv, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def _patch_roster(monkeypatch, rows):
    """Make both list_agents call sites return AgentInfo for *rows*."""
    agents = [AgentInfo.from_dict(r) for r in rows]
    monkeypatch.setattr(sm_mod, "list_agents", lambda *a, **k: list(agents))
    monkeypatch.setattr(state_mod, "list_agents", lambda *a, **k: list(agents))


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


def test_no_command_errors() -> None:
    """Invoking with no subcommand exits non-zero (argparse required=True)."""
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code != 0


# ---------------------------------------------------------------------------
# status / start
# ---------------------------------------------------------------------------


def test_status_text(monkeypatch) -> None:
    _patch_roster(monkeypatch, [_bg_row("aaaa1111", "alpha"), _bg_row("bbbb2222", "bravo")])
    code, out, _ = _run(["status"])
    assert code == 0
    assert "2 manager(s)" in out
    assert "alpha [aaaa1111]" in out
    assert "bravo [bbbb2222]" in out


def test_status_json(monkeypatch) -> None:
    _patch_roster(monkeypatch, [_bg_row("aaaa1111", "alpha", state="blocked", waitingFor="permission prompt")])
    code, out, _ = _run(["status", "--json"])
    assert code == 0
    data = json.loads(out)
    assert data["count"] == 1
    m = data["managers"][0]
    assert m["id"] == "aaaa1111"
    assert m["name"] == "alpha"
    assert m["state"] == "blocked"
    assert m["needs_attention"] is True


def test_status_filter_by_key(monkeypatch) -> None:
    _patch_roster(monkeypatch, [_bg_row("aaaa1111", "alpha"), _bg_row("bbbb2222", "bravo")])
    code, out, _ = _run(["status", "bravo", "--json"])
    assert code == 0
    data = json.loads(out)
    assert data["count"] == 1
    assert data["managers"][0]["name"] == "bravo"


def test_status_filter_miss_returns_2(monkeypatch) -> None:
    _patch_roster(monkeypatch, [_bg_row("aaaa1111", "alpha")])
    code, out, err = _run(["status", "ghost"])
    assert code == 2
    assert "no manager matching" in err


def test_status_empty_roster(monkeypatch) -> None:
    _patch_roster(monkeypatch, [])
    code, out, _ = _run(["status"])
    assert code == 0
    assert "no managers" in out


def test_status_merges_emitted_status(monkeypatch, tmp_path) -> None:
    """A manager's on-disk status.json is merged into the CLI status view."""
    _patch_roster(monkeypatch, [_bg_row("aaaa1111", "alpha")])
    write_status(
        ManagerStatus(state="HUMAN_GATE", summary="needs the robot", name="alpha"),
        base_dir=tmp_path,
    )
    code, out, _ = _run(["status", "--status-dir", str(tmp_path), "--json"])
    assert code == 0
    m = json.loads(out)["managers"][0]
    assert m["report"]["state"] == "HUMAN_GATE"
    assert m["needs_attention"] is True
    assert m["headline"] == "needs the robot"


def test_start_reports_reconnect(monkeypatch) -> None:
    _patch_roster(monkeypatch, [_bg_row("aaaa1111", "alpha")])
    code, out, _ = _run(["start"])
    assert code == 0
    assert "reconnected to 1 background manager(s)" in out
    assert "alpha [aaaa1111]" in out


# ---------------------------------------------------------------------------
# spawn
# ---------------------------------------------------------------------------


def test_spawn_text(monkeypatch) -> None:
    _patch_roster(monkeypatch, [])  # empty roster for reconnect

    def fake_spawn(cls, task, *, name, **kwargs):
        return FleetManager(session_id=f"newid00-{name}", id="newid00", name=name, cwd=kwargs.get("cwd"))

    monkeypatch.setattr(sm_mod.FleetManager, "spawn", classmethod(fake_spawn))
    code, out, _ = _run(["spawn", "do the thing", "--name", "worker1"])
    assert code == 0
    assert "spawned 'worker1' [newid00]" in out


def test_spawn_json_passes_args(monkeypatch) -> None:
    _patch_roster(monkeypatch, [])
    captured: dict = {}

    def fake_spawn(cls, task, *, name, **kwargs):
        captured["task"] = task
        captured["name"] = name
        captured.update(kwargs)
        return FleetManager(session_id="sid-1", id="newid00", name=name)

    monkeypatch.setattr(sm_mod.FleetManager, "spawn", classmethod(fake_spawn))
    code, out, _ = _run(
        [
            "spawn",
            "build it",
            "--name",
            "w2",
            "--cwd",
            "/tmp/p",
            "--model",
            "sonnet",
            "--mcp-config",
            "a.json",
            "--mcp-config",
            "b.json",
            "-w",
            "--json",
        ]
    )
    assert code == 0
    data = json.loads(out)
    assert data["id"] == "newid00"
    assert captured["task"] == "build it"
    assert captured["name"] == "w2"
    assert captured["cwd"] == "/tmp/p"
    assert captured["model"] == "sonnet"
    assert captured["mcp_configs"] == ["a.json", "b.json"]
    assert captured["worktree"] is True


def test_spawn_duplicate_name_fails(monkeypatch) -> None:
    """Spawning a name already on the roster is rejected (exit 1)."""
    _patch_roster(monkeypatch, [_bg_row("aaaa1111", "alpha")])

    def fake_spawn(cls, *a, **k):  # must not be reached
        raise AssertionError("spawn should not be called for a duplicate name")

    monkeypatch.setattr(sm_mod.FleetManager, "spawn", classmethod(fake_spawn))
    code, out, err = _run(["spawn", "task", "--name", "alpha"])
    assert code == 1
    assert "alpha" in err


# ---------------------------------------------------------------------------
# logs
# ---------------------------------------------------------------------------


def test_logs_text(monkeypatch) -> None:
    _patch_roster(monkeypatch, [_bg_row("aaaa1111", "alpha")])
    monkeypatch.setattr(cli, "tail_logs", lambda agent_id, **k: ["line one", "line two"])
    code, out, _ = _run(["logs", "alpha"])
    assert code == 0
    assert "line one" in out and "line two" in out


def test_logs_json(monkeypatch) -> None:
    _patch_roster(monkeypatch, [_bg_row("aaaa1111", "alpha")])
    captured: dict = {}

    def fake_tail(agent_id, *, lines=40, **k):
        captured["id"] = agent_id
        captured["lines"] = lines
        return ["hi"]

    monkeypatch.setattr(cli, "tail_logs", fake_tail)
    code, out, _ = _run(["logs", "alpha", "--lines", "5", "--json"])
    assert code == 0
    data = json.loads(out)
    assert data["id"] == "aaaa1111"
    assert data["lines"] == ["hi"]
    assert captured == {"id": "aaaa1111", "lines": 5}


def test_logs_unknown_manager(monkeypatch) -> None:
    _patch_roster(monkeypatch, [_bg_row("aaaa1111", "alpha")])
    code, out, err = _run(["logs", "ghost"])
    assert code == 2
    assert "no manager matching" in err


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


def test_stop_one(monkeypatch) -> None:
    _patch_roster(monkeypatch, [_bg_row("aaaa1111", "alpha")])
    calls = {"stop": [], "rm": []}
    monkeypatch.setattr(FleetManager, "stop", lambda self, **k: calls["stop"].append(self.id))
    monkeypatch.setattr(FleetManager, "stop_and_remove", lambda self, **k: calls["rm"].append(self.id))
    code, out, _ = _run(["stop", "alpha"])
    assert code == 0
    assert calls["stop"] == ["aaaa1111"]
    assert calls["rm"] == []
    assert "stopped 1 manager(s): aaaa1111" in out


def test_stop_with_rm(monkeypatch) -> None:
    _patch_roster(monkeypatch, [_bg_row("aaaa1111", "alpha")])
    calls = {"rm": []}
    monkeypatch.setattr(FleetManager, "stop_and_remove", lambda self, **k: calls["rm"].append(self.id))
    code, out, _ = _run(["stop", "alpha", "--rm", "--json"])
    assert code == 0
    data = json.loads(out)
    assert data == {"action": "stopped+removed", "ids": ["aaaa1111"]}
    assert calls["rm"] == ["aaaa1111"]


def test_stop_all(monkeypatch) -> None:
    _patch_roster(monkeypatch, [_bg_row("aaaa1111", "alpha"), _bg_row("bbbb2222", "bravo")])
    stopped = []
    monkeypatch.setattr(FleetManager, "stop", lambda self, **k: stopped.append(self.id))
    code, out, _ = _run(["stop", "--all"])
    assert code == 0
    assert set(stopped) == {"aaaa1111", "bbbb2222"}


def test_stop_needs_key_or_all(monkeypatch) -> None:
    _patch_roster(monkeypatch, [_bg_row("aaaa1111", "alpha")])
    code, out, err = _run(["stop"])
    assert code == 2
    assert "needs a manager key or --all" in err


def test_stop_unknown_manager(monkeypatch) -> None:
    _patch_roster(monkeypatch, [_bg_row("aaaa1111", "alpha")])
    code, out, err = _run(["stop", "ghost"])
    assert code == 2
    assert "no manager matching" in err


# ---------------------------------------------------------------------------
# Integration — REAL bg session driven entirely through the CLI
# ---------------------------------------------------------------------------

_HAS_CLAUDE = shutil.which("claude") is not None


@pytest.mark.skipif(not _HAS_CLAUDE, reason="claude CLI not installed")
def test_real_cli_end_to_end(tmp_path) -> None:
    """spawn → status → logs → stop --rm, all through cli.main, then assert clean."""
    name = f"u6-cli-{uuid.uuid4().hex[:8]}"
    spawned_id = None
    try:
        # spawn (JSON so we can capture the short id)
        code, out, err = _run(["spawn", "Reply with the single word DONE and nothing else.",
                               "--name", name, "--cwd", str(tmp_path), "--json"])
        assert code == 0, f"spawn failed: {err}"
        spawned = json.loads(out)
        spawned_id = spawned["id"]
        assert spawned["name"] == name

        # status shows it (filter by our unique name so output is scoped)
        code, out, err = _run(["status", name, "--all", "--json"])
        assert code == 0, f"status failed: {err}"
        managers = json.loads(out)["managers"]
        assert any(m["id"] == spawned_id for m in managers), out

        # logs is read-only and must not error
        code, out, err = _run(["logs", name, "--lines", "5"])
        assert code == 0, f"logs failed: {err}"

        # stop + remove through the CLI
        code, out, err = _run(["stop", name, "--rm", "--json"])
        assert code == 0, f"stop failed: {err}"
        assert spawned_id in json.loads(out)["ids"]
    finally:
        # Belt-and-suspenders cleanup: never leave a stray session even if an
        # assertion above failed before the CLI stop ran.
        if spawned_id is not None:
            FleetManager(session_id="x", id=spawned_id, name=name).stop_and_remove()
        remaining = [
            a for a in sm_mod.list_agents(include_all=False)
            if a.name == name
        ]
        assert remaining == [], f"manager {name} still active after teardown"
