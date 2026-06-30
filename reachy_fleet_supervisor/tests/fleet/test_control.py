"""Tests for the interactive control surface (U12).

Covers the pure steering-argv assembly (the decision #16 ``claude --resume
<sessionId> -p`` path), the :class:`FleetController` actions over a
:class:`SessionManager` (send / pause / resume / kill, with the manager's
subprocess calls faked so no real ``claude`` runs), and the remote-control
guard. A REAL bg steering round-trip lives in ``test_manager.py`` /
``test_cli.py`` (skipped without the CLI).
"""

from __future__ import annotations
import subprocess

import pytest

from reachy_fleet_supervisor.fleet import (
    ControlResult,
    FleetManager,
    FleetController,
    SessionManager,
    build_steer_argv,
)
from reachy_fleet_supervisor.fleet import manager as manager_mod
from reachy_fleet_supervisor.fleet.manager import FleetManagerError


# ---------------------------------------------------------------------------
# build_steer_argv (pure) — the verified decision #16 steering path
# ---------------------------------------------------------------------------


def test_build_steer_argv_shape() -> None:
    """The steer argv is `claude --resume <sessionId> --permission-mode … -p <msg>`."""
    argv = build_steer_argv("sess-uuid-1234", "approve and continue")
    assert argv == [
        "claude",
        "--resume",
        "sess-uuid-1234",
        "--permission-mode",
        "bypassPermissions",
        "-p",
        "approve and continue",
    ]
    # The prompt + message are always last (never swallowed by an option).
    assert argv[-2:] == ["-p", "approve and continue"]
    # We resume the SAME conversation — never branch a new session.
    assert "--fork-session" not in argv


def test_build_steer_argv_optionals() -> None:
    """model / output_format / extra_args flow in before the trailing prompt."""
    argv = build_steer_argv(
        "sid",
        "hi",
        permission_mode="acceptEdits",
        model="claude-x",
        output_format="json",
        extra_args=["--add-dir", "/tmp"],
        claude_bin="/usr/bin/claude",
    )
    assert argv[0] == "/usr/bin/claude"
    assert "--permission-mode" in argv and argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert ["--model", "claude-x"] == argv[argv.index("--model"):argv.index("--model") + 2]
    assert ["--output-format", "json"] == argv[argv.index("--output-format"):argv.index("--output-format") + 2]
    assert "--add-dir" in argv
    assert argv[-2:] == ["-p", "hi"]


@pytest.mark.parametrize("sid,msg", [("", "hi"), ("  ", "hi"), ("sid", ""), ("sid", "   ")])
def test_build_steer_argv_validates(sid, msg) -> None:
    with pytest.raises(FleetManagerError):
        build_steer_argv(sid, msg)


def test_build_steer_argv_rejects_bad_permission_mode() -> None:
    with pytest.raises(FleetManagerError):
        build_steer_argv("sid", "hi", permission_mode="nonsense")


# ---------------------------------------------------------------------------
# FleetController — actions over a SessionManager (subprocess faked)
# ---------------------------------------------------------------------------


def _bg_manager(short_id: str, name: str, *, cwd: str = "/proj") -> FleetManager:
    return FleetManager(
        session_id=f"{short_id}-1111-2222-3333-444444444444",
        id=short_id,
        name=name,
        cwd=cwd,
        run_mode="background",
    )


@pytest.fixture
def fake_claude(monkeypatch):
    """Record every `_run_claude` call and return a canned reply; no subprocess."""
    calls: list[list[str]] = []

    def _fake(args, *, claude_bin="claude", cwd=None, timeout=None, check=True):
        calls.append(list(args))
        return subprocess.CompletedProcess(list(args), 0, stdout="PONG from manager\n", stderr="")

    monkeypatch.setattr(manager_mod, "_run_claude", _fake)
    return calls


def _controller_with(*managers: FleetManager) -> FleetController:
    sm = SessionManager()
    for m in managers:
        sm.adopt(m)
    return FleetController(sm)


def test_send_steers_via_resume(fake_claude) -> None:
    """send → claude --resume <sessionId> -p <message>; reply comes back in detail."""
    mgr = _bg_manager("aaaa1111", "alpha")
    ctl = _controller_with(mgr)
    result = ctl.send("alpha", "please approve the gate")
    assert isinstance(result, ControlResult)
    assert result.ok is True
    assert result.action == "send" and result.manager_id == "aaaa1111"
    assert "PONG" in result.detail
    # In-place steer (decision #16): `claude stop <id>` hands the live bg session
    # back FIRST, THEN resume-by-FULL-session-id continues it with the message last.
    assert fake_claude == [
        ["stop", mgr.id],
        ["--resume", mgr.session_id, "--permission-mode", "bypassPermissions",
         "-p", "please approve the gate"],
    ]


def test_send_unknown_key(fake_claude) -> None:
    ctl = _controller_with(_bg_manager("aaaa1111", "alpha"))
    result = ctl.send("ghost", "hello")
    assert result.ok is False
    assert "no manager matching" in result.detail
    assert fake_claude == []  # never shelled out


def test_send_empty_message_rejected(fake_claude) -> None:
    ctl = _controller_with(_bg_manager("aaaa1111", "alpha"))
    result = ctl.send("alpha", "   ")
    assert result.ok is False
    assert "must not be empty" in result.detail
    assert fake_claude == []


def test_send_remote_control_directs_to_claude_ai(fake_claude) -> None:
    """A remote-control manager is steered from claude.ai, not the fleet shell."""
    rc = FleetManager(
        session_id="rc-1111-2222-3333-444444444444",
        id="rc",
        name="phone",
        run_mode="remote-control",
        process=None,
    )
    ctl = _controller_with(rc)
    result = ctl.send("phone", "do the thing")
    assert result.ok is False
    assert "remote-control" in result.detail and "claude.ai" in result.detail
    assert fake_claude == []  # never tried to --resume an RC session


def test_pause_calls_stop(fake_claude) -> None:
    ctl = _controller_with(_bg_manager("aaaa1111", "alpha"))
    result = ctl.pause("alpha")
    assert result.ok is True and "paused" in result.detail
    assert fake_claude == [["stop", "aaaa1111"]]


def test_resume_calls_respawn(fake_claude) -> None:
    ctl = _controller_with(_bg_manager("aaaa1111", "alpha"))
    result = ctl.resume("alpha")
    assert result.ok is True and "resumed" in result.detail
    assert fake_claude == [["respawn", "aaaa1111"]]


def test_resume_remote_control_rejected(fake_claude) -> None:
    rc = FleetManager(session_id="rc-x", id="rc", name="phone", run_mode="remote-control")
    ctl = _controller_with(rc)
    result = ctl.resume("phone")
    assert result.ok is False and "remote-control" in result.detail


def test_kill_stops_removes_and_forgets(fake_claude) -> None:
    mgr = _bg_manager("aaaa1111", "alpha")
    ctl = _controller_with(mgr)
    result = ctl.kill("alpha")
    assert result.ok is True and "killed" in result.detail
    # stop_and_remove → claude stop then claude rm, and it is forgotten.
    assert fake_claude == [["stop", "aaaa1111"], ["rm", "aaaa1111"]]
    assert ctl.session_manager.get("alpha") is None


def test_apply_dispatches(fake_claude) -> None:
    ctl = _controller_with(_bg_manager("aaaa1111", "alpha"))
    assert ctl.apply("pause", "alpha").ok is True
    assert ctl.apply("send", "alpha", "hi").ok is True
    bad = ctl.apply("frobnicate", "alpha")
    assert bad.ok is False and "unknown action" in bad.detail


def test_actions_list() -> None:
    ctl = _controller_with(_bg_manager("aaaa1111", "alpha"))
    assert ctl.actions == ("send", "pause", "resume", "kill")


def test_control_result_to_dict_roundtrip() -> None:
    r = ControlResult(True, "send", "alpha", "ok", manager_id="aaaa1111", extra={"reply": "x"})
    d = r.to_dict()
    assert d["ok"] is True and d["action"] == "send" and d["manager_id"] == "aaaa1111"
    assert d["reply"] == "x"
