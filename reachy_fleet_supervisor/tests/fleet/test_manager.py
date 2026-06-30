"""Tests for FleetManager — spawn/control of a ``claude --bg`` manager (U2).

Pure-logic tests (argv building, output parsing) run everywhere. The single
integration test spawns a REAL trivial background session, asserts it appears
in ``claude agents --json``, then ALWAYS cleans it up (stop + rm). It is skipped
when the ``claude`` CLI is not installed.
"""

from __future__ import annotations
import uuid
import shutil

import pytest

from reachy_fleet_supervisor.fleet import (
    RUN_MODE_ENV,
    DEFAULT_RUN_MODE,
    PERMISSION_MODE_ENV,
    DEFAULT_PERMISSION_MODE,
    AgentInfo,
    FleetManager,
    FleetManagerError,
    list_agents,
    short_id_for,
    build_spawn_argv,
    resolve_run_mode,
    parse_agents_json,
    validate_run_mode,
    parse_spawn_output,
    resolve_permission_mode,
    validate_permission_mode,
    build_remote_control_server_argv,
)


# ---------------------------------------------------------------------------
# Pure helpers — argv building
# ---------------------------------------------------------------------------


def test_build_spawn_argv_minimal() -> None:
    """Minimal spawn argv has the required --bg flags and the task last."""
    sid = "11111111-2222-3333-4444-555555555555"
    argv = build_spawn_argv("do a thing", name="mgr", session_id=sid)
    assert argv[0] == "claude"
    assert "--bg" in argv
    assert argv[argv.index("--name") + 1] == "mgr"
    assert argv[argv.index("--session-id") + 1] == sid
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert argv[-1] == "do a thing"  # task/prompt is always the final positional


def test_build_spawn_argv_optionals() -> None:
    """Model, mcp configs, worktree name, and extra args are all wired in order."""
    argv = build_spawn_argv(
        "task",
        name="m",
        session_id="abc",
        permission_mode="acceptEdits",
        model="claude-sonnet-4-6",
        mcp_configs=["a.json", "b.json"],
        worktree="feature",
        extra_args=["--add-dir", "/x"],
    )
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[argv.index("--model") + 1] == "claude-sonnet-4-6"
    assert argv.count("--mcp-config") == 2
    assert "a.json" in argv and "b.json" in argv
    assert argv[argv.index("-w") + 1] == "feature"
    assert "--add-dir" in argv and "/x" in argv
    assert argv[-1] == "task"


def test_default_permission_mode_is_non_interactive() -> None:
    """The default must be a mode that does NOT prompt — a bg manager has no TTY.

    Regression for the U10 deadlock: spawning with ``default``/``acceptEdits``
    leaves a background manager ``blocked`` ``waitingFor: permission prompt``.
    """
    assert DEFAULT_PERMISSION_MODE == "bypassPermissions"


def test_build_spawn_argv_rejects_invalid_permission_mode() -> None:
    """An invalid permission mode fails loudly in-process, not mid-spawn."""
    with pytest.raises(FleetManagerError):
        build_spawn_argv("t", name="m", session_id="s", permission_mode="yolo")


def test_validate_permission_mode_accepts_known_and_rejects_unknown() -> None:
    for mode in ("acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan"):
        assert validate_permission_mode(mode) == mode
    with pytest.raises(FleetManagerError):
        validate_permission_mode("nope")


def test_resolve_permission_mode_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit override > $FLEET_PERMISSION_MODE > built-in default."""
    monkeypatch.delenv(PERMISSION_MODE_ENV, raising=False)
    # default
    assert resolve_permission_mode() == DEFAULT_PERMISSION_MODE
    assert resolve_permission_mode("") == DEFAULT_PERMISSION_MODE
    # env wins over default
    monkeypatch.setenv(PERMISSION_MODE_ENV, "acceptEdits")
    assert resolve_permission_mode() == "acceptEdits"
    # explicit override wins over env
    assert resolve_permission_mode("plan") == "plan"
    # invalid (from any source) still raises
    monkeypatch.setenv(PERMISSION_MODE_ENV, "bogus")
    with pytest.raises(FleetManagerError):
        resolve_permission_mode()


def test_build_spawn_argv_worktree_true_is_bare_flag() -> None:
    """worktree=True emits a bare -w (auto-named worktree), no value."""
    argv = build_spawn_argv("t", name="m", session_id="s", worktree=True)
    idx = argv.index("-w")
    # nothing consumed after -w except the trailing task
    assert argv[idx + 1] == "t"


def test_build_spawn_argv_rejects_empty_task_name_session() -> None:
    """Empty task, name, or session_id are rejected up front."""
    with pytest.raises(FleetManagerError):
        build_spawn_argv("  ", name="m", session_id="s")
    with pytest.raises(FleetManagerError):
        build_spawn_argv("t", name="", session_id="s")
    with pytest.raises(FleetManagerError):
        build_spawn_argv("t", name="m", session_id="")


# ---------------------------------------------------------------------------
# run_mode knob (U11) — argv assembly + validation + resolution
# ---------------------------------------------------------------------------


def test_build_spawn_argv_defaults_to_background() -> None:
    """No run_mode given → the durable --bg backbone (unchanged default)."""
    argv = build_spawn_argv("t", name="m", session_id="s")
    assert "--bg" in argv
    assert "--remote-control" not in argv


def test_build_spawn_argv_remote_control_form() -> None:
    """run_mode=remote-control → `claude --remote-control <name> … <task>`, NO --bg."""
    sid = "11111111-2222-3333-4444-555555555555"
    argv = build_spawn_argv(
        "do a thing",
        name="phone",
        session_id=sid,
        run_mode="remote-control",
        remote_control_name_prefix="reacher",
        model="claude-sonnet-4-6",
        mcp_configs=["a.json"],
    )
    assert "--bg" not in argv, "remote-control must NOT compose with --bg"
    # --remote-control takes the manager name as its (optional) session name.
    assert argv[argv.index("--remote-control") + 1] == "phone"
    assert argv[argv.index("--session-id") + 1] == sid
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert argv[argv.index("--remote-control-session-name-prefix") + 1] == "reacher"
    assert argv[argv.index("--model") + 1] == "claude-sonnet-4-6"
    assert "--mcp-config" in argv and "a.json" in argv
    assert argv[-1] == "do a thing"  # task is still the final positional


def test_build_spawn_argv_rejects_invalid_run_mode() -> None:
    with pytest.raises(FleetManagerError):
        build_spawn_argv("t", name="m", session_id="s", run_mode="server")


def test_validate_run_mode_accepts_known_and_rejects_unknown() -> None:
    for mode in ("background", "remote-control"):
        assert validate_run_mode(mode) == mode
    with pytest.raises(FleetManagerError):
        validate_run_mode("nope")


def test_resolve_run_mode_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit override > $FLEET_RUN_MODE > built-in default (background)."""
    monkeypatch.delenv(RUN_MODE_ENV, raising=False)
    assert resolve_run_mode() == DEFAULT_RUN_MODE == "background"
    assert resolve_run_mode("") == "background"
    monkeypatch.setenv(RUN_MODE_ENV, "remote-control")
    assert resolve_run_mode() == "remote-control"
    assert resolve_run_mode("background") == "background"  # explicit wins over env
    monkeypatch.setenv(RUN_MODE_ENV, "bogus")
    with pytest.raises(FleetManagerError):
        resolve_run_mode()


def test_build_remote_control_server_argv_form() -> None:
    """Server mode: `claude remote-control --spawn worktree --capacity N …` (no task)."""
    argv = build_remote_control_server_argv(
        name="host", spawn="worktree", capacity=3, name_prefix="reacher"
    )
    assert argv[:2] == ["claude", "remote-control"]
    assert "--bg" not in argv
    assert argv[argv.index("--spawn") + 1] == "worktree"
    assert argv[argv.index("--capacity") + 1] == "3"
    assert argv[argv.index("--name") + 1] == "host"
    assert argv[argv.index("--remote-control-session-name-prefix") + 1] == "reacher"
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"


def test_build_remote_control_server_argv_rejects_bad_spawn_and_capacity() -> None:
    with pytest.raises(FleetManagerError):
        build_remote_control_server_argv(spawn="nope")
    with pytest.raises(FleetManagerError):
        build_remote_control_server_argv(capacity=0)


# ---------------------------------------------------------------------------
# Remote-control spawn path (U11) — launched via an injected launcher, NO live RC
# ---------------------------------------------------------------------------


class _FakeProcess:
    """Stand-in for the launched RC ``Popen`` (poll/terminate/wait/kill)."""

    def __init__(self) -> None:
        self._alive = True
        self.terminated = False

    def poll(self):
        return None if self._alive else 0

    def terminate(self) -> None:
        self.terminated = True
        self._alive = False

    def wait(self, timeout: float | None = None) -> int:
        return 0

    def kill(self) -> None:
        self._alive = False


def test_spawn_remote_control_uses_injected_launcher_and_records_argv() -> None:
    """The RC spawn assembles the verified argv and launches it (no subprocess)."""
    seen: dict = {}
    proc = _FakeProcess()

    def fake_launcher(argv, *, claude_bin, cwd):
        seen["argv"] = list(argv)
        seen["cwd"] = cwd
        return proc

    sid = "abcd1234-0000-0000-0000-000000000000"
    mgr = FleetManager.spawn_remote_control(
        "ship it",
        name="phone",
        cwd="/proj",
        session_id=sid,
        launcher=fake_launcher,
    )
    assert mgr.run_mode == "remote-control"
    assert mgr.is_remote_control
    assert mgr.id == "abcd1234"
    assert mgr.process is proc
    # The launched argv is the verified interactive RC command.
    assert "--remote-control" in seen["argv"] and "--bg" not in seen["argv"]
    assert seen["argv"][-1] == "ship it"
    assert seen["cwd"] == "/proj"


def test_spawn_dispatches_to_remote_control_without_subprocess() -> None:
    """FleetManager.spawn(run_mode='remote-control') routes to the RC launch path."""
    proc = _FakeProcess()
    mgr = FleetManager.spawn(
        "task",
        name="phone",
        run_mode="remote-control",
        launcher=lambda argv, *, claude_bin, cwd: proc,
    )
    assert mgr.is_remote_control and mgr.process is proc


def test_remote_control_manager_is_not_roster_visible() -> None:
    """An RC manager is never in `claude agents --json` → info() is None."""
    mgr = FleetManager(
        session_id="abcd1234-x", id="abcd1234", name="phone", run_mode="remote-control"
    )
    assert mgr.info() is None  # short-circuits before any CLI call


def test_remote_control_is_running_tracks_process() -> None:
    proc = _FakeProcess()
    mgr = FleetManager(
        session_id="s", id="abcd1234", name="phone", run_mode="remote-control", process=proc
    )
    assert mgr.is_running() is True
    proc.terminate()
    assert mgr.is_running() is False


def test_remote_control_stop_terminates_process_no_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """stop()/stop_and_remove() on an RC manager terminate the process, never `claude stop`."""
    import reachy_fleet_supervisor.fleet.manager as mgr_mod

    def _boom(*a, **k):  # any CLI call would be wrong for an RC session
        raise AssertionError("RC teardown must not shell out to `claude`")

    monkeypatch.setattr(mgr_mod, "_run_claude", _boom)
    proc = _FakeProcess()
    mgr = FleetManager(
        session_id="s", id="abcd1234", name="phone", run_mode="remote-control", process=proc
    )
    assert mgr.stop() is None
    assert proc.terminated
    mgr.stop_and_remove()  # idempotent, still no CLI


# ---------------------------------------------------------------------------
# Pure helpers — short id + spawn output parsing
# ---------------------------------------------------------------------------


def test_short_id_for_is_uuid_first_segment() -> None:
    """The short id is the first dash-delimited segment of the session UUID."""
    sid = "197d4014-d18f-4465-b46b-a3845e8a11a6"
    assert short_id_for(sid) == "197d4014"


def test_parse_spawn_output_typical() -> None:
    """Parses the real CLI confirmation line (middle-dot separated)."""
    stdout = (
        "Starting background service…\n"
        "backgrounded · 197d4014 · my-manager\n"
        "  claude agents             list sessions\n"
        "  claude attach 197d4014    open in this terminal\n"
    )
    short_id, name = parse_spawn_output(stdout)
    assert short_id == "197d4014"
    assert name == "my-manager"


def test_parse_spawn_output_without_name() -> None:
    """A confirmation line with no name yields the short id and None."""
    short_id, name = parse_spawn_output("backgrounded · deadbeef")
    assert short_id == "deadbeef"
    assert name is None


def test_parse_spawn_output_missing_marker_raises() -> None:
    """Output without a 'backgrounded' marker line is an error."""
    with pytest.raises(FleetManagerError):
        parse_spawn_output("some unrelated output\nno marker here\n")


# ---------------------------------------------------------------------------
# Pure helpers — agents --json parsing
# ---------------------------------------------------------------------------


_AGENTS_JSON = """
[
  {"pid": 10884, "cwd": "C:\\\\Source\\\\reacher", "kind": "interactive",
   "startedAt": 1782625026389, "sessionId": "23448782-82af-4999-a18c-c21a1215f033",
   "status": "idle"},
  {"pid": 17284, "id": "197d4014", "cwd": "C:\\\\Source\\\\reacher", "kind": "background",
   "startedAt": 1782626558056, "sessionId": "197d4014-d18f-4465-b46b-a3845e8a11a6",
   "name": "probe", "status": "busy", "state": "working", "waitingFor": "permission prompt"}
]
"""


def test_parse_agents_json_shapes() -> None:
    """Background vs interactive rows map to the right AgentInfo fields."""
    agents = parse_agents_json(_AGENTS_JSON)
    assert len(agents) == 2
    interactive, background = agents
    assert interactive.kind == "interactive"
    assert not interactive.is_background
    assert interactive.id is None
    assert background.is_background
    assert background.id == "197d4014"
    assert background.name == "probe"
    assert background.state == "working"
    assert background.waiting_for == "permission prompt"
    assert background.session_id == "197d4014-d18f-4465-b46b-a3845e8a11a6"


def test_parse_agents_json_empty_array() -> None:
    """An empty roster parses to an empty list."""
    assert parse_agents_json("[]") == []


def test_parse_agents_json_bad_json_raises() -> None:
    """Non-JSON output raises a FleetManagerError."""
    with pytest.raises(FleetManagerError):
        parse_agents_json("not json")


def test_parse_agents_json_non_array_raises() -> None:
    """A JSON object (not an array) is rejected."""
    with pytest.raises(FleetManagerError):
        parse_agents_json('{"sessionId": "x"}')


def test_agentinfo_from_dict_defaults() -> None:
    """A minimal row yields sensible None defaults and is not background."""
    info = AgentInfo.from_dict({"sessionId": "s"})
    assert info.session_id == "s"
    assert info.id is None and info.name is None and info.is_background is False


# ---------------------------------------------------------------------------
# FleetManager handle behaviour (no subprocess)
# ---------------------------------------------------------------------------


def test_spawn_resolves_real_session_id_from_roster(monkeypatch) -> None:
    """spawn() trusts the stdout short id and resolves the REAL sessionId from the roster.

    Mirrors verified behaviour: ``claude --bg`` mints its own session id, so the
    requested ``--session-id`` must NOT be assumed. The short id from stdout is
    authoritative; the full sessionId comes from ``claude agents --json``.
    """
    captured: dict = {}
    # The CLI minted a different session id than the one we requested.
    real_session = "8e3f4839-89c7-40c6-80fb-a2d6488bcbd9"
    real_short = "8e3f4839"

    class _Proc:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout
            self.stderr = ""
            self.returncode = 0

    def fake_run(args, *, claude_bin="claude", cwd=None, timeout=0.0, check=True):
        args = list(args)
        if args[:2] == ["agents", "--json"]:
            return _Proc(
                f'[{{"id": "{real_short}", "sessionId": "{real_session}", '
                f'"kind": "background", "name": "m", "cwd": "/proj"}}]'
            )
        # spawn call
        captured["spawn_args"] = args
        captured["cwd"] = cwd
        return _Proc(f"backgrounded · {real_short} · m\n")

    monkeypatch.setattr("reachy_fleet_supervisor.fleet.manager._run_claude", fake_run)
    requested = uuid.UUID("197d4014-d18f-4465-b46b-a3845e8a11a6")
    monkeypatch.setattr("reachy_fleet_supervisor.fleet.manager.uuid.uuid4", lambda: requested)

    mgr = FleetManager.spawn("build it", name="m", cwd="/proj")
    assert mgr.id == real_short
    assert mgr.session_id == real_session  # resolved, not the requested UUID
    assert mgr.name == "m"
    assert mgr.cwd == "/proj"
    assert mgr.task == "build it"
    # We still pass the requested session id through (honored on setups that support it).
    assert str(requested) in captured["spawn_args"]
    assert captured["spawn_args"][-1] == "build it"
    assert captured["cwd"] == "/proj"


def test_spawn_raises_when_session_never_appears(monkeypatch) -> None:
    """If the spawned row never shows in the roster, spawn() raises (no silent handle)."""

    class _Proc:
        def __init__(self, stdout: str) -> None:
            self.stdout = stdout
            self.stderr = ""
            self.returncode = 0

    def fake_run(args, *, claude_bin="claude", cwd=None, timeout=0.0, check=True):
        if list(args)[:2] == ["agents", "--json"]:
            return _Proc("[]")  # never appears
        return _Proc("backgrounded · abcd1234 · m\n")

    monkeypatch.setattr("reachy_fleet_supervisor.fleet.manager._run_claude", fake_run)
    with pytest.raises(FleetManagerError):
        FleetManager.spawn("t", name="m", resolve_retries=2, resolve_delay=0.0)


# ---------------------------------------------------------------------------
# Integration — REAL trivial background session, always cleaned up
# ---------------------------------------------------------------------------

_HAS_CLAUDE = shutil.which("claude") is not None


@pytest.mark.skipif(not _HAS_CLAUDE, reason="claude CLI not installed")
def test_real_bg_session_lifecycle(tmp_path) -> None:
    """Spawn a real bg manager, assert it shows in `claude agents --json`, then clean up."""
    name = f"u2-test-{uuid.uuid4().hex[:8]}"
    mgr = None
    try:
        mgr = FleetManager.spawn(
            "Reply with the single word DONE and do nothing else.",
            name=name,
            cwd=tmp_path,
        )
        assert mgr.id == short_id_for(mgr.session_id)
        assert len(mgr.session_id.split("-")) == 5  # a real UUID

        # It must appear in the agent roster as a background session.
        agents = list_agents(include_all=True)
        match = next((a for a in agents if a.session_id == mgr.session_id), None)
        assert match is not None, f"spawned manager {mgr.session_id} not found in agents --json"
        assert match.is_background
        assert match.name == name

        # info() resolves the same row.
        info = mgr.info()
        assert info is not None and info.session_id == mgr.session_id
    finally:
        if mgr is not None:
            mgr.stop_and_remove()
            # Verify it is gone from the active roster (not in non-completed list).
            remaining = [a for a in list_agents(include_all=False) if a.session_id == mgr.session_id]
            assert remaining == [], f"manager {mgr.session_id} still active after teardown"
