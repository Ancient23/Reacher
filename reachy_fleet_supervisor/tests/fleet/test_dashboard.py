"""Tests for the read-only web dashboard (U7).

The dashboard is a FastAPI renderer over a :class:`FleetState`. These tests build
a state directly (no real ``claude`` CLI needed), apply fake roster rows + logs +
emitted status, then serve the app with Starlette's ``TestClient`` and assert
both the HTML (``GET /``) and the JSON poll endpoint (``GET /api/fleet``). Pure
serialization / HTML-rendering helpers are also unit-tested. One end-to-end test
drives the dashboard against a REAL trivial background session and ALWAYS tears
it down; it is skipped when the ``claude`` CLI is not installed.
"""

from __future__ import annotations
import uuid
import shutil

import pytest


pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from reachy_fleet_supervisor.fleet import (  # noqa: E402
    AgentInfo,
    FleetState,
    FleetPoller,
    ManagerStatus,
    SessionManager,
    render_page,
    snapshot_payload,
    create_dashboard_app,
)
from reachy_fleet_supervisor.fleet import state as state_mod  # noqa: E402
from reachy_fleet_supervisor.fleet.dashboard import render_cards  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _agent(short_id: str, name: str, **extra: object) -> AgentInfo:
    data = {
        "id": short_id,
        "sessionId": f"{short_id}-1111-2222-3333-444444444444",
        "kind": "background",
        "name": name,
        "cwd": "/proj",
        "state": "working",
        "status": "busy",
    }
    data.update(extra)
    return AgentInfo.from_dict(data)


def _state_with(*agents: AgentInfo, logs=None, statuses=None) -> FleetState:
    state = FleetState()
    state.apply(list(agents), logs=logs or {}, statuses=statuses or {}, now=1000.0)
    return state


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def test_snapshot_payload_includes_managers_and_transcript() -> None:
    """snapshot_payload mirrors the CLI JSON and includes transcript buffers."""
    state = _state_with(
        _agent("aaa111", "alice"),
        logs={"aaa111": ["line one", "line two"]},
    )
    payload = snapshot_payload(state.snapshot)
    assert payload["count"] == 1
    assert payload["updated_at"] == 1000.0
    mgr = payload["managers"][0]
    assert mgr["id"] == "aaa111"
    assert mgr["name"] == "alice"
    assert mgr["transcript"] == ["line one", "line two"]
    assert mgr["last_line"] == "line two"


# ---------------------------------------------------------------------------
# JSON endpoint
# ---------------------------------------------------------------------------


def test_api_fleet_serves_current_snapshot() -> None:
    """GET /api/fleet returns the live FleetState as JSON."""
    state = _state_with(
        _agent("aaa111", "alice"),
        _agent("bbb222", "bob", state="blocked", waitingFor="permission prompt"),
        logs={"aaa111": ["working on it"]},
    )
    client = TestClient(create_dashboard_app(state))
    resp = client.get("/api/fleet")
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 2
    names = {m["name"] for m in data["managers"]}
    assert names == {"alice", "bob"}
    bob = next(m for m in data["managers"] if m["name"] == "bob")
    assert bob["needs_attention"] is True
    assert bob["waiting_for"] == "permission prompt"


def test_api_fleet_reflects_state_updates() -> None:
    """Re-polling the endpoint reflects a later FleetState.apply()."""
    state = _state_with(_agent("aaa111", "alice"))
    client = TestClient(create_dashboard_app(state))
    assert client.get("/api/fleet").json()["count"] == 1
    state.apply(
        [_agent("aaa111", "alice"), _agent("bbb222", "bob")], now=1001.0
    )
    assert client.get("/api/fleet").json()["count"] == 2


def test_api_fleet_merges_emitted_status() -> None:
    """A manager's emitted ManagerStatus (decision #21) surfaces in the JSON."""
    state = _state_with(
        _agent("aaa111", "alice"),
        statuses={
            "aaa111": ManagerStatus(
                state="HUMAN_GATE", summary="needs a decision", name="alice"
            )
        },
    )
    client = TestClient(create_dashboard_app(state))
    mgr = client.get("/api/fleet").json()["managers"][0]
    assert mgr["needs_attention"] is True
    assert mgr["headline"] == "needs a decision"
    assert mgr["report"]["state"] == "HUMAN_GATE"


# ---------------------------------------------------------------------------
# HTML page
# ---------------------------------------------------------------------------


def test_index_renders_card_grid() -> None:
    """GET / renders an HTML grid with one card per manager."""
    state = _state_with(_agent("aaa111", "alice"), _agent("bbb222", "bob"))
    client = TestClient(create_dashboard_app(state))
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    # Two server-rendered cards (the live-poll JS template uses `card${...}`,
    # so the literal `card"` only appears on rendered cards).
    assert render_cards(state.snapshot).count('<article class="card') == 2
    assert 'data-key="aaa111"' in body and 'data-key="bbb222"' in body
    assert "alice" in body and "bob" in body
    assert 'id="grid"' in body
    assert "api/fleet" in body  # the live-poll script targets the JSON endpoint


def test_card_renders_identity_color() -> None:
    """Each server-rendered card carries its manager's identity color (U9)."""
    state = _state_with(_agent("aaa111", "alice"), _agent("bbb222", "bob"))
    cards = render_cards(state.snapshot)
    snap = state.snapshot
    for m in snap.managers:
        assert m.color is not None
        assert f"--agent:{m.color.hex}" in cards
        assert f'title="{m.color.name}"' in cards
    assert cards.count('class="dot"') == 2


def test_api_fleet_includes_color() -> None:
    state = _state_with(_agent("aaa111", "alice"))
    mgr = TestClient(create_dashboard_app(state)).get("/api/fleet").json()["managers"][0]
    assert mgr["color"] is not None
    assert {"name", "hex", "ansi"} <= set(mgr["color"])


def test_index_empty_state() -> None:
    """With no managers the page shows the empty-state message, not a card."""
    page = render_page(FleetState().snapshot, title="T", poll_ms=2000)
    assert "No managers running." in page
    # No server-rendered card markup (the JS template literal is not counted).
    assert "<article" not in render_cards(FleetState().snapshot)


def test_card_escapes_html() -> None:
    """Manager fields are HTML-escaped (no injection through name/logs)."""
    state = _state_with(
        _agent("aaa111", "<script>x</script>"),
        logs={"aaa111": ["<b>danger</b>"]},
    )
    cards = render_cards(state.snapshot)
    assert "<script>x</script>" not in cards
    assert "&lt;script&gt;" in cards
    assert "<b>danger</b>" not in cards


def test_attention_card_marked() -> None:
    """A blocked/waiting manager gets the attention CSS class."""
    cards = render_cards(_state_with(_agent("bbb222", "bob", state="blocked")).snapshot)
    assert "card attention" in cards


def test_healthz() -> None:
    """Liveness probe responds ok."""
    client = TestClient(create_dashboard_app(FleetState()))
    assert client.get("/healthz").json() == {"ok": True}


# ---------------------------------------------------------------------------
# on_request refresh hook
# ---------------------------------------------------------------------------


def test_on_request_hook_refreshes_before_read() -> None:
    """on_request is invoked before each read (poll-on-demand path)."""
    state = FleetState()
    calls = {"n": 0}

    def refresh() -> None:
        calls["n"] += 1
        state.apply([_agent("aaa111", "alice")], now=2000.0 + calls["n"])

    client = TestClient(create_dashboard_app(state, on_request=refresh))
    assert client.get("/api/fleet").json()["count"] == 1
    assert calls["n"] == 1
    client.get("/")
    assert calls["n"] == 2


def test_on_request_error_is_swallowed() -> None:
    """A failing on_request hook never breaks the read; last snapshot serves."""
    state = _state_with(_agent("aaa111", "alice"))

    def boom() -> None:
        raise RuntimeError("poll failed")

    client = TestClient(create_dashboard_app(state, on_request=boom))
    resp = client.get("/api/fleet")
    assert resp.status_code == 200
    assert resp.json()["count"] == 1


# ---------------------------------------------------------------------------
# Integration — a REAL background session rendered through the dashboard
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
def test_dashboard_renders_real_bg_session() -> None:
    """Spawn a REAL trivial bg manager, poll it into FleetState, see it on /.

    Always stops + removes the session, even on failure (no stray agents).
    """
    name = f"u7-dash-{uuid.uuid4().hex[:8]}"
    sm = SessionManager()
    mgr = None
    try:
        mgr = sm.spawn("Reply with the single word DONE and do nothing else.", name=name)
        state = FleetState()
        poller = FleetPoller(
            state,
            predicate=lambda a: a.session_id == mgr.session_id,
            include_all=True,
        )
        poller.poll_once()
        # The spawned manager should now be visible through the dashboard.
        client = TestClient(create_dashboard_app(state))
        data = client.get("/api/fleet").json()
        assert any(m["name"] == name for m in data["managers"])
        assert name in client.get("/").text
    finally:
        if mgr is not None:
            mgr.stop_and_remove()
            # Confirm it is gone from the roster (no stray background sessions).
            remaining = [
                a for a in state_mod.list_agents(include_all=False)
                if a.session_id == mgr.session_id
            ]
            assert remaining == [], f"manager {mgr.session_id} still active after teardown"
