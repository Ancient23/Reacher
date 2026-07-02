"""Tests for SessionManager — track N managers + reconnect on restart (U3).

Logic tests (tracking, lookup, mocked-roster reconnect) run everywhere. The
single integration test spawns a REAL trivial background session, builds a
*fresh* SessionManager to simulate an app restart, asserts it reconnects to that
session from the live roster, then ALWAYS cleans it up. It is skipped when the
``claude`` CLI is not installed.
"""

from __future__ import annotations
import json
import uuid
import shutil
from pathlib import Path

import pytest

from reachy_fleet_supervisor.fleet import (
    AgentInfo,
    FleetConfig,
    FleetManager,
    FleetManagerError,
    RetryPolicy,
    SessionManager,
    parse_fleet_config,
)
from reachy_fleet_supervisor.fleet import session_manager as sm_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _handle(short_id: str, name: str, *, session_id: str | None = None) -> FleetManager:
    """Build a FleetManager handle directly (no subprocess) for registry tests."""
    return FleetManager(
        session_id=session_id or f"{short_id}-0000-0000-0000-000000000000",
        id=short_id,
        name=name,
    )


def _bg_row(short_id: str, name: str, **extra: object) -> dict[str, object]:
    """A `claude agents --json` background row."""
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


def _interactive_row(session_id: str) -> dict[str, object]:
    return {"sessionId": session_id, "kind": "interactive", "status": "idle"}


# ---------------------------------------------------------------------------
# Tracking + lookup (no subprocess)
# ---------------------------------------------------------------------------


def test_adopt_tracks_and_indexes() -> None:
    """Adopted handles are tracked, counted, and looked up by id or name."""
    sm = SessionManager()
    a = sm.adopt(_handle("aaaa1111", "alpha"))
    b = sm.adopt(_handle("bbbb2222", "bravo"))

    assert len(sm) == 2
    assert sm.ids() == ["aaaa1111", "bbbb2222"]
    assert sm.names() == ["alpha", "bravo"]
    assert sm.managers == [a, b]
    assert list(sm) == [a, b]

    assert sm.get("aaaa1111") is a           # by short id
    assert sm.get("bravo") is b              # by name
    assert sm.get("nope") is None
    assert "alpha" in sm and "aaaa1111" in sm and "ghost" not in sm


def test_adopt_is_idempotent_by_id() -> None:
    """Re-adopting the same short id replaces the handle, not duplicates it."""
    sm = SessionManager()
    sm.adopt(_handle("aaaa1111", "alpha"))
    replacement = sm.adopt(_handle("aaaa1111", "alpha-renamed"))
    assert len(sm) == 1
    assert sm.get("aaaa1111") is replacement
    assert sm.names() == ["alpha-renamed"]


def test_require_raises_when_absent() -> None:
    sm = SessionManager()
    with pytest.raises(KeyError):
        sm.require("missing")


def test_get_ambiguous_name_raises() -> None:
    """If two managers share a name, a name lookup is ambiguous (use the id)."""
    sm = SessionManager()
    sm.adopt(_handle("aaaa1111", "dup"))
    sm.adopt(_handle("bbbb2222", "dup"))
    with pytest.raises(FleetManagerError):
        sm.get("dup")
    # The unambiguous short ids still resolve.
    assert sm.get("aaaa1111").id == "aaaa1111"
    assert sm.get("bbbb2222").id == "bbbb2222"


def test_forget_drops_without_stopping() -> None:
    sm = SessionManager()
    sm.adopt(_handle("aaaa1111", "alpha"))
    dropped = sm.forget("alpha")
    assert dropped is not None and dropped.id == "aaaa1111"
    assert len(sm) == 0
    assert sm.forget("alpha") is None  # already gone


# ---------------------------------------------------------------------------
# from_agent_info (reconnect building block)
# ---------------------------------------------------------------------------


def test_from_agent_info_builds_background_handle() -> None:
    info = AgentInfo.from_dict(_bg_row("cccc3333", "charlie"))
    mgr = FleetManager.from_agent_info(info, task="resumed")
    assert mgr.id == "cccc3333"
    assert mgr.session_id == info.session_id
    assert mgr.name == "charlie"
    assert mgr.cwd == "/proj"
    assert mgr.task == "resumed"


def test_from_agent_info_name_falls_back_to_id() -> None:
    info = AgentInfo.from_dict({"id": "dddd4444", "sessionId": "dddd4444-x", "kind": "background"})
    assert FleetManager.from_agent_info(info).name == "dddd4444"


def test_from_agent_info_rejects_non_background() -> None:
    """An interactive row (no short id) cannot be a fleet manager."""
    info = AgentInfo.from_dict(_interactive_row("eeee5555-aaaa"))
    with pytest.raises(FleetManagerError):
        FleetManager.from_agent_info(info)


# ---------------------------------------------------------------------------
# reconnect (mocked roster)
# ---------------------------------------------------------------------------


def _patch_roster(monkeypatch, rows: list[dict[str, object]]) -> None:
    """Make session_manager.list_agents return AgentInfo for *rows*."""
    agents = [AgentInfo.from_dict(r) for r in rows]
    monkeypatch.setattr(
        sm_mod, "list_agents", lambda *a, **k: list(agents)
    )


def test_reconnect_adopts_background_only(monkeypatch) -> None:
    """reconnect() wraps background rows and ignores interactive ones."""
    _patch_roster(
        monkeypatch,
        [
            _interactive_row("11111111-aaaa"),
            _bg_row("aaaa1111", "alpha"),
            _bg_row("bbbb2222", "bravo"),
        ],
    )
    sm = SessionManager()
    adopted = sm.reconnect()
    assert {m.id for m in adopted} == {"aaaa1111", "bbbb2222"}
    assert len(sm) == 2
    assert sm.get("alpha").session_id == "aaaa1111-1111-2222-3333-444444444444"


def test_reconnect_is_idempotent(monkeypatch) -> None:
    """A second reconnect over the same roster adopts nothing new."""
    _patch_roster(monkeypatch, [_bg_row("aaaa1111", "alpha")])
    sm = SessionManager()
    assert len(sm.reconnect()) == 1
    assert sm.reconnect() == []   # already tracked
    assert len(sm) == 1


def test_reconnect_respects_predicate(monkeypatch) -> None:
    """A predicate scopes which background rows are adopted."""
    _patch_roster(
        monkeypatch,
        [
            _bg_row("aaaa1111", "mine", cwd="/proj/a"),
            _bg_row("bbbb2222", "theirs", cwd="/other"),
        ],
    )
    sm = SessionManager()
    adopted = sm.reconnect(predicate=lambda a: a.cwd == "/proj/a")
    assert [m.id for m in adopted] == ["aaaa1111"]
    assert sm.get("theirs") is None


def test_reconnect_after_restart_simulated(monkeypatch) -> None:
    """A brand-new SessionManager re-adopts a session it never spawned itself.

    Simulates an app restart: process memory is gone, but the durable bg session
    is still in the roster, so a fresh SessionManager reconnects to it.
    """
    _patch_roster(monkeypatch, [_bg_row("aaaa1111", "alpha")])

    # "Before restart": one SessionManager spawned/tracked it (modelled as adopt).
    before = SessionManager()
    before.adopt(FleetManager.from_agent_info(AgentInfo.from_dict(_bg_row("aaaa1111", "alpha"))))
    spawned_session = before.get("alpha").session_id

    # "After restart": a new, empty SessionManager rebuilds from the same roster.
    after = SessionManager.from_roster()
    reconnected = after.get("alpha")
    assert reconnected is not None
    assert reconnected.session_id == spawned_session
    assert after.ids() == ["aaaa1111"]


def test_spawn_rejects_duplicate_name(monkeypatch) -> None:
    """Spawning a second manager with an in-use name is rejected before shelling out."""
    sm = SessionManager()
    sm.adopt(_handle("aaaa1111", "alpha"))

    called = {"spawn": 0}

    def fake_spawn(*a, **k):  # must not be reached
        called["spawn"] += 1
        return _handle("zzzz9999", "alpha")

    monkeypatch.setattr(sm_mod.FleetManager, "spawn", classmethod(lambda cls, *a, **k: fake_spawn(*a, **k)))
    with pytest.raises(FleetManagerError):
        sm.spawn("task", name="alpha")
    assert called["spawn"] == 0


def test_spawn_tracks_via_session_manager(monkeypatch) -> None:
    """spawn() forwards to FleetManager.spawn, passes claude_bin, and tracks the handle."""
    captured: dict = {}

    def fake_spawn(cls, task, *, name, claude_bin="claude", **kwargs):
        captured["task"] = task
        captured["name"] = name
        captured["claude_bin"] = claude_bin
        return _handle("ffff6666", name)

    monkeypatch.setattr(sm_mod.FleetManager, "spawn", classmethod(fake_spawn))
    sm = SessionManager(claude_bin="claude-test")
    mgr = sm.spawn("build it", name="newone")
    assert mgr.id == "ffff6666"
    assert sm.get("newone") is mgr
    assert captured == {"task": "build it", "name": "newone", "claude_bin": "claude-test"}


def test_spawn_project_materializes_mcp_config_and_resolves_defaults(
    tmp_path, monkeypatch
) -> None:
    """spawn_project() wires a project's mcp[] through --mcp-config (U18)."""
    cfg = parse_fleet_config(
        {
            "permission_mode": "bypassPermissions",
            "run_mode": "background",
            "projects": [
                {
                    "name": "unreal_proj",
                    "path": str(tmp_path / "repo"),
                    "mcp": [
                        {"name": "unreal", "command": "uv", "args": ["run", "unreal-mcp"]},
                    ],
                    "defaults": {"model": "claude-sonnet-4-6"},
                },
            ],
        }
    )

    captured: dict = {}

    def fake_spawn(cls, task, *, name, claude_bin="claude", **kwargs):
        captured["task"] = task
        captured["name"] = name
        captured.update(kwargs)
        return _handle("aaaa1111", name)

    monkeypatch.setattr(sm_mod.FleetManager, "spawn", classmethod(fake_spawn))
    sm = SessionManager()
    mgr = sm.spawn_project(
        cfg, "unreal_proj", "drive unreal", name="ue-worker", mcp_config_dir=tmp_path / "mcp"
    )

    assert mgr.id == "aaaa1111"
    assert captured["task"] == "drive unreal"
    assert str(captured["cwd"]) == str((tmp_path / "repo"))
    assert captured["run_mode"] == "background"
    assert captured["permission_mode"] == "bypassPermissions"
    assert captured["model"] == "claude-sonnet-4-6"
    assert len(captured["mcp_configs"]) == 1
    mcp_path = Path(captured["mcp_configs"][0])
    assert mcp_path.is_file()
    assert json.loads(mcp_path.read_text(encoding="utf-8")) == {
        "mcpServers": {"unreal": {"command": "uv", "args": ["run", "unreal-mcp"]}}
    }


def test_spawn_project_without_mcp_servers_passes_none(tmp_path, monkeypatch) -> None:
    """A project with no mcp[] spawns with an empty mcp_configs list — no hard dependency."""
    cfg = parse_fleet_config(
        {"projects": [{"name": "plain", "path": str(tmp_path / "repo")}]}
    )

    captured: dict = {}

    def fake_spawn(cls, task, *, name, claude_bin="claude", **kwargs):
        captured.update(kwargs)
        return _handle("bbbb2222", name)

    monkeypatch.setattr(sm_mod.FleetManager, "spawn", classmethod(fake_spawn))
    sm = SessionManager()
    sm.spawn_project(cfg, "plain", "task", name="w", mcp_config_dir=tmp_path / "mcp")
    assert captured["mcp_configs"] == []


def test_spawn_project_materializes_ollama_backend_settings(tmp_path, monkeypatch) -> None:
    """spawn_project() writes the effective backend's env to .claude/settings.json (U19)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = parse_fleet_config(
        {
            "projects": [
                {
                    "name": "local_proj",
                    "path": str(repo),
                    "defaults": {
                        "backend": {
                            "kind": "ollama",
                            "ollama": {"base_url": "http://localhost:11434", "model": "llama3.1"},
                        }
                    },
                }
            ]
        }
    )

    def fake_spawn(cls, task, *, name, claude_bin="claude", **kwargs):
        return _handle("cccc3333", name)

    monkeypatch.setattr(sm_mod.FleetManager, "spawn", classmethod(fake_spawn))
    sm = SessionManager()
    sm.spawn_project(cfg, "local_proj", "task", name="w", mcp_config_dir=tmp_path / "mcp")

    settings_path = repo / ".claude" / "settings.json"
    assert settings_path.is_file()
    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert data["env"]["ANTHROPIC_BASE_URL"] == "http://localhost:11434"
    assert data["env"]["ANTHROPIC_MODEL"] == "llama3.1"


def test_spawn_project_default_backend_writes_no_settings_file(tmp_path, monkeypatch) -> None:
    """The default 'claude' backend does not create a settings.json override."""
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = parse_fleet_config({"projects": [{"name": "plain2", "path": str(repo)}]})

    def fake_spawn(cls, task, *, name, claude_bin="claude", **kwargs):
        return _handle("dddd4444", name)

    monkeypatch.setattr(sm_mod.FleetManager, "spawn", classmethod(fake_spawn))
    sm = SessionManager()
    sm.spawn_project(cfg, "plain2", "task", name="w", mcp_config_dir=tmp_path / "mcp")

    assert not (repo / ".claude" / "settings.json").exists()


# ---------------------------------------------------------------------------
# control (mocked)
# ---------------------------------------------------------------------------


def test_stop_and_remove_forgets(monkeypatch) -> None:
    """stop_and_remove() tears the session down and drops it from the registry."""
    sm = SessionManager()
    mgr = sm.adopt(_handle("aaaa1111", "alpha"))
    calls = {"stop_and_remove": 0}
    monkeypatch.setattr(type(mgr), "stop_and_remove", lambda self, **k: calls.__setitem__("stop_and_remove", calls["stop_and_remove"] + 1))
    sm.stop_and_remove("alpha")
    assert calls["stop_and_remove"] == 1
    assert len(sm) == 0


def test_stop_and_remove_all_clears(monkeypatch) -> None:
    sm = SessionManager()
    sm.adopt(_handle("aaaa1111", "alpha"))
    sm.adopt(_handle("bbbb2222", "bravo"))
    monkeypatch.setattr(FleetManager, "stop_and_remove", lambda self, **k: None)
    sm.stop_and_remove_all()
    assert len(sm) == 0


# ---------------------------------------------------------------------------
# Integration — REAL reconnect-after-restart, always cleaned up
# ---------------------------------------------------------------------------

_HAS_CLAUDE = shutil.which("claude") is not None


@pytest.mark.skipif(not _HAS_CLAUDE, reason="claude CLI not installed")
def test_real_reconnect_after_restart(tmp_path) -> None:
    """Spawn a REAL bg manager, then a FRESH SessionManager reconnects to it.

    This is the U3 acceptance: tracking + reconnect-after-restart against the
    live daemon roster. The original handle always tears the session down.
    """
    name = f"u3-test-{uuid.uuid4().hex[:8]}"
    first = SessionManager()
    mgr = None
    try:
        mgr = first.spawn(
            "Reply with the single word DONE and do nothing else.",
            name=name,
            cwd=tmp_path,
        )
        assert first.get(name) is mgr

        # Simulate an app restart: a brand-new SessionManager with empty memory
        # rebuilds itself from the live roster. Scope to our unique name so we
        # never adopt (and later tear down) unrelated background agents.
        after_restart = SessionManager()
        adopted = after_restart.reconnect(include_all=True, predicate=lambda a: a.name == name)
        assert any(m.session_id == mgr.session_id for m in adopted), (
            f"fresh SessionManager did not reconnect to {mgr.session_id}"
        )
        reconnected = after_restart.get(name)
        assert reconnected is not None
        assert reconnected.id == mgr.id
        assert reconnected.session_id == mgr.session_id

        # Reconnect is idempotent (same scope adopts nothing new).
        assert after_restart.reconnect(include_all=True, predicate=lambda a: a.name == name) == []
    finally:
        if mgr is not None:
            mgr.stop_and_remove()
            remaining = [
                a for a in sm_mod.list_agents(include_all=False)
                if a.session_id == mgr.session_id
            ]
            assert remaining == [], f"manager {mgr.session_id} still active after teardown"


# ---------------------------------------------------------------------------
# Rate-limit backoff + recovery wiring (U23)
# ---------------------------------------------------------------------------


def test_spawn_retries_on_rate_limit_then_succeeds(monkeypatch) -> None:
    """spawn() retries a rate-limit-shaped FleetManager.spawn error with backoff.

    ``base_delay=0.0`` keeps the real (unmocked) ``time.sleep`` calls instant.
    """
    calls = {"n": 0}

    def fake_spawn(cls, task, *, name, claude_bin="claude", **kwargs):
        calls["n"] += 1
        if calls["n"] < 3:
            raise FleetManagerError("429 too many requests — rate limit")
        return _handle("cccc3333", name)

    monkeypatch.setattr(sm_mod.FleetManager, "spawn", classmethod(fake_spawn))

    sm = SessionManager()
    mgr = sm.spawn(
        "task",
        name="flaky",
        retry_policy=RetryPolicy(max_attempts=5, base_delay=0.0, jitter=0.0),
    )
    assert mgr.id == "cccc3333"
    assert calls["n"] == 3


def test_spawn_does_not_retry_non_rate_limit_error(monkeypatch) -> None:
    calls = {"n": 0}

    def fake_spawn(cls, task, *, name, claude_bin="claude", **kwargs):
        calls["n"] += 1
        raise FleetManagerError("manager task/prompt must not be empty")

    monkeypatch.setattr(sm_mod.FleetManager, "spawn", classmethod(fake_spawn))
    sm = SessionManager()
    with pytest.raises(FleetManagerError, match="must not be empty"):
        sm.spawn("task", name="bad", retry_policy=RetryPolicy(max_attempts=5))
    assert calls["n"] == 1


def test_spawn_retry_policy_none_disables_retry(monkeypatch) -> None:
    calls = {"n": 0}

    def fake_spawn(cls, task, *, name, claude_bin="claude", **kwargs):
        calls["n"] += 1
        raise FleetManagerError("rate limit exceeded")

    monkeypatch.setattr(sm_mod.FleetManager, "spawn", classmethod(fake_spawn))
    sm = SessionManager()
    with pytest.raises(FleetManagerError, match="rate limit"):
        sm.spawn("task", name="once", retry_policy=None)
    assert calls["n"] == 1


def test_send_message_retries_on_rate_limit(monkeypatch) -> None:
    handle = _handle("dddd4444", "worker")
    calls = {"n": 0}

    def fake_send(self, message, **kwargs):
        calls["n"] += 1
        if calls["n"] < 2:
            raise FleetManagerError("overloaded, please retry")
        return "reply"

    monkeypatch.setattr(sm_mod.FleetManager, "send_message", fake_send)
    sm = SessionManager()
    sm.adopt(handle)
    result = sm.send_message(
        "worker", "hello", retry_policy=RetryPolicy(max_attempts=3, base_delay=0.0, jitter=0.0)
    )
    assert result == "reply"
    assert calls["n"] == 2


def test_health_classifies_from_manager_info(monkeypatch) -> None:
    handle = _handle("eeee5555", "worker")
    monkeypatch.setattr(
        sm_mod.FleetManager, "info",
        lambda self, **kwargs: AgentInfo(session_id=self.session_id, id=self.id, state="failed"),
    )
    sm = SessionManager()
    sm.adopt(handle)
    health = sm.health("worker")
    assert health.status == "recoverable"
    assert health.needs_recovery is True


def test_recover_respawns_a_recoverable_manager(monkeypatch) -> None:
    handle = _handle("ffff7777", "worker")
    resumed = {"n": 0}
    monkeypatch.setattr(
        sm_mod.FleetManager, "info",
        lambda self, **kwargs: AgentInfo(session_id=self.session_id, id=self.id, state="stopped"),
    )

    def fake_resume(self, **kwargs):
        resumed["n"] += 1

    monkeypatch.setattr(sm_mod.FleetManager, "resume", fake_resume)
    sm = SessionManager()
    sm.adopt(handle)
    assert sm.recover("worker") is True
    assert resumed["n"] == 1


def test_recover_is_noop_for_healthy_manager(monkeypatch) -> None:
    handle = _handle("gggg8888", "worker")
    monkeypatch.setattr(
        sm_mod.FleetManager, "info",
        lambda self, **kwargs: AgentInfo(session_id=self.session_id, id=self.id, state="working"),
    )
    resumed = {"n": 0}
    monkeypatch.setattr(sm_mod.FleetManager, "resume", lambda self, **k: resumed.__setitem__("n", resumed["n"] + 1))
    sm = SessionManager()
    sm.adopt(handle)
    assert sm.recover("worker") is False
    assert resumed["n"] == 0


def test_recover_all_sweeps_every_tracked_manager(monkeypatch) -> None:
    healthy = _handle("h0000001", "healthy-one")
    recoverable = _handle("h0000002", "recoverable-one")

    def fake_info(self, **kwargs):
        state = "working" if self.id == "h0000001" else "failed"
        return AgentInfo(session_id=self.session_id, id=self.id, state=state)

    monkeypatch.setattr(sm_mod.FleetManager, "info", fake_info)
    monkeypatch.setattr(sm_mod.FleetManager, "resume", lambda self, **k: None)

    sm = SessionManager()
    sm.adopt(healthy)
    sm.adopt(recoverable)
    recovered = sm.recover_all()
    assert recovered == ["h0000002"]
