"""App wiring — the running app actually serves /fleet AND a voice-spawned
manager becomes fleet-visible (U10 regression).

U10's first on-robot run failed not in any isolated component (those all pass)
but in the *integration wiring* of the real running app:

1. ``main.run`` never mounted the ``/fleet`` dashboard nor started a
   :class:`FleetPoller`, so the dashboard URL 404'd and state never refreshed;
2. the only voice coding tool ran an in-process worker that is **not** a
   ``claude --bg`` agent, so it never appeared in the roster the poller reads —
   the voice spawn path and the fleet observability path were disconnected.

These tests pin down the seam that fixes both — :mod:`reachy_fleet_supervisor.fleet.runtime`
(the shared runtime + the exact ``mount_fleet_dashboard`` call ``main.run``
makes) and the ``spawn_manager`` voice tool — without needing a robot or a real
``claude`` CLI. The full on-robot loop (real voice → real ``claude --bg`` →
browser) remains the HUMAN gate in ``docs/roadmap/STATE.yaml``.
"""

from __future__ import annotations
import asyncio
import importlib.util
from pathlib import Path

import pytest


pytest.importorskip("fastapi")
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from reachy_fleet_supervisor.fleet import (  # noqa: E402
    AgentInfo,
    FleetState,
    FleetPoller,
    FleetRuntime,
    SessionManager,
    get_fleet_runtime,
    set_fleet_runtime,
    build_fleet_runtime,
    create_dashboard_app,
    mount_fleet_dashboard,
)
from reachy_fleet_supervisor.fleet import runtime as runtime_mod  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bg(short_id: str, name: str, state: str = "working") -> AgentInfo:
    return AgentInfo.from_dict(
        {
            "id": short_id,
            "sessionId": f"{short_id}-0000-0000-0000-000000000000",
            "kind": "background",
            "name": name,
            "cwd": "/proj",
            "state": state,
            "status": state,
        }
    )


def _interactive(short_id: str, name: str) -> AgentInfo:
    return AgentInfo.from_dict(
        {
            "id": short_id,
            "sessionId": f"{short_id}-0000-0000-0000-000000000000",
            "kind": "interactive",
            "name": name,
            "cwd": "/proj",
            "state": "working",
        }
    )


def _load_spawn_manager_cls():
    """Load the SpawnManager voice tool straight from the profile file."""
    here = Path(__file__).resolve()
    src = here.parents[2] / "src"
    tool_file = (
        src
        / "reachy_fleet_supervisor"
        / "profiles"
        / "_reachy_fleet_supervisor_locked_profile"
        / "spawn_manager.py"
    )
    spec = importlib.util.spec_from_file_location("rfs_spawn_manager_undertest", tool_file)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SpawnManager


@pytest.fixture(autouse=True)
def _clean_runtime():
    """Ensure no process-wide runtime leaks across tests (stops any poller)."""
    set_fleet_runtime(None)
    yield
    set_fleet_runtime(None)


# ---------------------------------------------------------------------------
# build_fleet_runtime — poller scoped to background managers
# ---------------------------------------------------------------------------


def test_build_runtime_poller_keeps_only_background_managers() -> None:
    rt = build_fleet_runtime(reconnect=False, fetch_logs=False)
    assert rt.poller.predicate is not None
    assert rt.poller.predicate(_bg("a1", "alpha"))
    assert not rt.poller.predicate(_interactive("i1", "term"))
    assert not rt.poller.running, "build must not start the poller"


def test_build_runtime_polls_with_all_to_retain_terminal_managers() -> None:
    # Disappearing-widget / missed-gate fix: the live poller must poll `claude
    # agents --json --all` so a manager that emits HUMAN_GATE/COMPLETE and then
    # EXITS stays in FleetState (its card survives + its gate is still spoken).
    rt = build_fleet_runtime(reconnect=False, fetch_logs=False)
    assert rt.poller.include_all is True


# ---------------------------------------------------------------------------
# mount_fleet_dashboard — exactly what main.run calls to serve /fleet
# ---------------------------------------------------------------------------


def test_mount_fleet_dashboard_serves_fleet_route_over_shared_state() -> None:
    state = FleetState()
    state.apply([_bg("a1", "alpha", "blocked")], now=1.0)
    rt = FleetRuntime(
        state=state,
        session_manager=SessionManager(),
        poller=FleetPoller(state, agents_source=lambda: []),
    )

    app = FastAPI()

    @app.get("/")
    def _root() -> dict[str, bool]:  # mimic settings_app's existing "/" route
        return {"ok": True}

    returned = mount_fleet_dashboard(app, runtime=rt)
    assert returned is rt

    client = TestClient(app)
    # The pre-existing "/" route still works — mounting /fleet didn't clobber it.
    assert client.get("/").json() == {"ok": True}
    # /fleet serves the dashboard JSON + HTML over the SAME FleetState.
    data = client.get("/fleet/api/fleet").json()
    assert [m["name"] for m in data["managers"]] == ["alpha"]
    assert "alpha" in client.get("/fleet/").text
    assert client.get("/fleet/healthz").json() == {"ok": True}


# ---------------------------------------------------------------------------
# get_fleet_runtime — a started, process-wide singleton
# ---------------------------------------------------------------------------


def test_get_fleet_runtime_returns_started_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    state = FleetState()
    rt = FleetRuntime(
        state=state,
        session_manager=SessionManager(),
        poller=FleetPoller(state, agents_source=lambda: [], interval=0.01),
    )
    monkeypatch.setattr(runtime_mod, "_BUILDER", lambda: rt)

    got = get_fleet_runtime()
    assert got is rt
    assert got.poller.running, "get_fleet_runtime must start the poller"
    assert get_fleet_runtime() is rt, "must be a singleton"

    set_fleet_runtime(None)  # teardown stops the poller
    assert not rt.poller.running


# ---------------------------------------------------------------------------
# THE regression: a voice-spawned manager becomes fleet-visible end-to-end
# ---------------------------------------------------------------------------


def test_voice_spawn_makes_manager_visible_on_dashboard(tmp_path: Path) -> None:
    """spawn_manager → SessionManager.spawn → roster → FleetState → dashboard.

    Uses a fake SessionManager whose ``spawn`` registers a background roster row
    (standing in for a real ``claude --bg`` agent) that the poller reads. Asserts
    the manager the *voice tool* spawned shows up in the shared FleetState and on
    the dashboard — the disconnect U10's on-robot run exposed.
    """
    roster: list[AgentInfo] = []

    class _FakeManager:
        def __init__(self, name: str, ident: str) -> None:
            self.name = name
            self.id = ident

    class _FakeSessionManager:
        def spawn(self, task: str, *, name: str, cwd: str, **kw: object) -> _FakeManager:
            assert task and name and cwd  # the tool must pass all three
            roster.append(_bg(name[:8], name, "working"))
            return _FakeManager(name, name[:8])

    state = FleetState()
    rt = FleetRuntime(
        state=state,
        session_manager=_FakeSessionManager(),  # type: ignore[arg-type]
        poller=FleetPoller(state, agents_source=lambda: list(roster)),
    )
    set_fleet_runtime(rt)

    SpawnManager = _load_spawn_manager_cls()
    out = asyncio.run(SpawnManager()(None, name="tester", path=str(tmp_path), task="run the tests"))

    assert out["status"] == "spawned"
    assert out["name"] == "tester"
    # The tool nudged an immediate poll → the spawned manager is in FleetState…
    assert "tester" in state.snapshot.names()
    # …and on the dashboard reading that same state.
    client = TestClient(create_dashboard_app(state))
    names = [m["name"] for m in client.get("/api/fleet").json()["managers"]]
    assert "tester" in names


def _capturing_runtime() -> tuple[FleetRuntime, dict]:
    """A runtime whose SessionManager records the kwargs spawn() was called with."""
    captured: dict = {}

    class _CapturingManager:
        def __init__(self, name: str) -> None:
            self.name = name
            self.id = name[:8]

    class _CapturingSessionManager:
        def spawn(self, task: str, *, name: str, cwd: str, permission_mode: str, **kw: object):
            captured["task"] = task
            captured["name"] = name
            captured["cwd"] = cwd
            captured["permission_mode"] = permission_mode
            captured["run_mode"] = kw.get("run_mode")
            return _CapturingManager(name)

    state = FleetState()
    rt = FleetRuntime(
        state=state,
        session_manager=_CapturingSessionManager(),  # type: ignore[arg-type]
        poller=FleetPoller(state, agents_source=lambda: []),
    )
    return rt, captured


def test_voice_spawn_defaults_to_non_interactive_permission_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """U10 deadlock fix: the voice tool spawns with bypassPermissions by default.

    A ``claude --bg`` manager has no TTY; a prompting mode leaves it ``blocked``
    ``waitingFor: permission prompt`` forever. The voice path must pass the
    non-interactive default through to SessionManager.spawn.
    """
    monkeypatch.delenv("FLEET_PERMISSION_MODE", raising=False)
    rt, captured = _capturing_runtime()
    set_fleet_runtime(rt)
    SpawnManager = _load_spawn_manager_cls()
    out = asyncio.run(SpawnManager()(None, name="tester", path=str(tmp_path), task="run tests"))
    assert out["status"] == "spawned"
    assert captured["permission_mode"] == "bypassPermissions"


def test_voice_spawn_permission_mode_override_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit permission_mode from the voice tool is honored."""
    monkeypatch.delenv("FLEET_PERMISSION_MODE", raising=False)
    rt, captured = _capturing_runtime()
    set_fleet_runtime(rt)
    SpawnManager = _load_spawn_manager_cls()
    out = asyncio.run(
        SpawnManager()(None, name="tester", path=str(tmp_path), task="t", permission_mode="acceptEdits")
    )
    assert out["status"] == "spawned"
    assert captured["permission_mode"] == "acceptEdits"


def test_voice_spawn_env_sets_default_permission_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """$FLEET_PERMISSION_MODE configures the default when no override is given."""
    monkeypatch.setenv("FLEET_PERMISSION_MODE", "dontAsk")
    rt, captured = _capturing_runtime()
    set_fleet_runtime(rt)
    SpawnManager = _load_spawn_manager_cls()
    asyncio.run(SpawnManager()(None, name="tester", path=str(tmp_path), task="t"))
    assert captured["permission_mode"] == "dontAsk"


def test_voice_spawn_rejects_invalid_permission_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bogus permission_mode is reported (spoken back), not spawned."""
    monkeypatch.delenv("FLEET_PERMISSION_MODE", raising=False)
    rt, captured = _capturing_runtime()
    set_fleet_runtime(rt)
    SpawnManager = _load_spawn_manager_cls()
    out = asyncio.run(
        SpawnManager()(None, name="tester", path=str(tmp_path), task="t", permission_mode="yolo")
    )
    assert "error" in out and "permission mode" in out["error"].lower()
    assert captured == {}  # never reached spawn


def test_voice_spawn_defaults_to_background_run_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No run_mode given → the durable, dashboard-visible background backbone."""
    monkeypatch.delenv("FLEET_RUN_MODE", raising=False)
    rt, captured = _capturing_runtime()
    set_fleet_runtime(rt)
    SpawnManager = _load_spawn_manager_cls()
    out = asyncio.run(SpawnManager()(None, name="tester", path=str(tmp_path), task="run tests"))
    assert out["status"] == "spawned"
    assert out["run_mode"] == "background"
    assert captured["run_mode"] == "background"


def test_voice_spawn_remote_control_run_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """run_mode='remote-control' flows through and the reply explains it won't be on the dashboard."""
    monkeypatch.delenv("FLEET_RUN_MODE", raising=False)
    rt, captured = _capturing_runtime()
    set_fleet_runtime(rt)
    SpawnManager = _load_spawn_manager_cls()
    out = asyncio.run(
        SpawnManager()(
            None, name="phone", path=str(tmp_path), task="watch this", run_mode="remote-control"
        )
    )
    assert out["status"] == "spawned"
    assert out["run_mode"] == "remote-control"
    assert captured["run_mode"] == "remote-control"
    assert "claude.ai" in out["result"] or "phone" in out["result"].lower()


def test_voice_spawn_rejects_invalid_run_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bogus run_mode is reported (spoken back), not spawned."""
    monkeypatch.delenv("FLEET_RUN_MODE", raising=False)
    rt, captured = _capturing_runtime()
    set_fleet_runtime(rt)
    SpawnManager = _load_spawn_manager_cls()
    out = asyncio.run(
        SpawnManager()(None, name="x", path=str(tmp_path), task="t", run_mode="server")
    )
    assert "error" in out and "run mode" in out["error"].lower()
    assert captured == {}  # never reached spawn


def test_spawn_manager_rejects_missing_project_path(tmp_path: Path) -> None:
    """A bad path is reported (spoken back), not silently spawned."""
    rt = FleetRuntime(
        state=FleetState(),
        session_manager=SessionManager(),
        poller=FleetPoller(FleetState(), agents_source=lambda: []),
    )
    set_fleet_runtime(rt)
    SpawnManager = _load_spawn_manager_cls()
    missing = str(tmp_path / "does_not_exist")
    out = asyncio.run(SpawnManager()(None, name="x", path=missing, task="do it"))
    assert "error" in out and "not found" in out["error"]
