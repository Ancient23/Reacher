"""Tests for the status convention — managers emit, the fleet reads (U5).

All pure/filesystem-backed (uses ``tmp_path``); no ``claude`` CLI needed. Covers
the :class:`ManagerStatus` record, the atomic write + best-effort read helpers,
the name->key mapping for FleetState, and the merge of an emitted status into
FleetState / FleetPoller so renderers see it.
"""

from __future__ import annotations

from reachy_fleet_supervisor.fleet import (
    AgentInfo,
    FleetPoller,
    FleetState,
    ManagerSnapshot,
    ManagerStatus,
    default_status_dir,
    parse_sentinel,
    read_status,
    read_status_for,
    read_statuses_for_agents,
    status_from_transcript,
    status_path_for,
    write_status,
)
from reachy_fleet_supervisor.fleet.status import STATUS_DIR_ENV


def _bg(short_id: str, name: str, **extra: object) -> AgentInfo:
    row = {
        "id": short_id,
        "sessionId": f"{short_id}-1111-2222-3333-444444444444",
        "kind": "background",
        "name": name,
        "cwd": "/proj",
        "state": "working",
        "status": "busy",
    }
    row.update(extra)
    return AgentInfo.from_dict(row)


# ---------------------------------------------------------------------------
# ManagerStatus — record semantics
# ---------------------------------------------------------------------------


def test_needs_attention_and_terminal_by_sentinel() -> None:
    assert ManagerStatus(state="HUMAN_GATE").needs_attention
    assert ManagerStatus(state="failed").needs_attention  # case-insensitive
    assert ManagerStatus(state="BLOCKED").needs_attention
    assert not ManagerStatus(state="CONTINUE").needs_attention
    assert not ManagerStatus(state="RUNNING").needs_attention
    assert ManagerStatus(state="COMPLETE").is_terminal
    assert ManagerStatus(state="FAILED").is_terminal
    assert not ManagerStatus(state="CONTINUE").is_terminal


def test_updated_at_excluded_from_equality() -> None:
    """A re-emit that only moves the timestamp is NOT a change (decision #21)."""
    a = ManagerStatus(state="CONTINUE", summary="did a thing", updated_at=1.0)
    b = ManagerStatus(state="CONTINUE", summary="did a thing", updated_at=999.0)
    c = ManagerStatus(state="CONTINUE", summary="did another thing", updated_at=1.0)
    assert a == b
    assert a != c


def test_to_from_dict_roundtrip_and_extra_folding() -> None:
    s = ManagerStatus(
        state="HUMAN_GATE",
        summary="needs robot",
        name="alpha",
        unit="U8",
        phase=2,
        iteration=3,
        gate="power on rear USB-C",
        extra={"repo": "reacher"},
    )
    data = s.to_dict()
    assert data["extra"] == {"repo": "reacher"}
    assert ManagerStatus.from_dict(data) == s

    # Unknown top-level keys fold into extra rather than blowing up.
    folded = ManagerStatus.from_dict({"state": "CONTINUE", "mystery": 42})
    assert folded.extra["mystery"] == 42
    assert folded.state == "CONTINUE"

    # Missing/empty state defaults to RUNNING.
    assert ManagerStatus.from_dict({"summary": "x"}).state == "RUNNING"
    assert ManagerStatus.from_dict({"state": ""}).state == "RUNNING"


def test_parse_sentinel_last_match_wins() -> None:
    text = "ROADMAP_STATE: CONTINUE\nblah\nROADMAP_STATE: HUMAN_GATE"
    assert parse_sentinel(text) == "HUMAN_GATE"
    assert parse_sentinel("no sentinel here") is None
    assert parse_sentinel("FOO_STATE: complete", sentinel_prefix="FOO_STATE") == "COMPLETE"


def test_status_from_transcript() -> None:
    """A drive-loop sentinel in the transcript tail derives a status (U13)."""
    tail = [
        "iteration 3: wrote tests",
        "ROADMAP_STATE: HUMAN_GATE",
    ]
    s = status_from_transcript(tail, name="alpha")
    assert s is not None
    assert s.state == "HUMAN_GATE"
    assert s.summary == "ROADMAP_STATE: HUMAN_GATE"
    assert s.name == "alpha"
    assert s.needs_attention
    # No sentinel -> None (nothing to derive).
    assert status_from_transcript(["just working", "no sentinel"]) is None
    # Custom prefix + last-match-wins.
    custom = status_from_transcript(
        ["LOOP_STATE: CONTINUE", "LOOP_STATE: COMPLETE"], sentinel_prefix="LOOP_STATE"
    )
    assert custom is not None and custom.state == "COMPLETE"


def test_from_report_line() -> None:
    s = ManagerStatus.from_report_line("ROADMAP_STATE: HUMAN_GATE", unit="U8")
    assert s.state == "HUMAN_GATE"
    assert s.unit == "U8"
    assert s.summary == "ROADMAP_STATE: HUMAN_GATE"
    # No sentinel -> RUNNING, line becomes the summary.
    plain = ManagerStatus.from_report_line("just working")
    assert plain.state == "RUNNING"
    assert plain.summary == "just working"


# ---------------------------------------------------------------------------
# Paths + default dir
# ---------------------------------------------------------------------------


def test_status_path_for(tmp_path) -> None:
    p = status_path_for("alpha", tmp_path)
    assert p == tmp_path / "alpha" / "status.json"


def test_default_status_dir_env_override(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(STATUS_DIR_ENV, str(tmp_path / "custom"))
    assert default_status_dir() == tmp_path / "custom"
    monkeypatch.delenv(STATUS_DIR_ENV, raising=False)
    assert default_status_dir().name == "status"  # ~/.reachy_fleet/status


# ---------------------------------------------------------------------------
# write_status / read_status — roundtrip, atomic, best-effort
# ---------------------------------------------------------------------------


def test_write_then_read_roundtrip(tmp_path) -> None:
    s = ManagerStatus(state="CONTINUE", summary="ok", name="alpha", unit="U5")
    path = write_status(s, base_dir=tmp_path)
    assert path == tmp_path / "alpha" / "status.json"
    assert path.is_file()
    assert read_status(path) == s
    assert read_status_for("alpha", tmp_path) == s


def test_write_status_overwrites_in_place(tmp_path) -> None:
    write_status(ManagerStatus(state="RUNNING", name="alpha"), base_dir=tmp_path)
    write_status(
        ManagerStatus(state="HUMAN_GATE", summary="waiting", name="alpha"),
        base_dir=tmp_path,
    )
    latest = read_status_for("alpha", tmp_path)
    assert latest.state == "HUMAN_GATE"
    assert latest.summary == "waiting"
    # No stray temp files left behind by the atomic write.
    assert [p.name for p in (tmp_path / "alpha").iterdir()] == ["status.json"]


def test_write_status_needs_a_name(tmp_path) -> None:
    import pytest

    with pytest.raises(ValueError):
        write_status(ManagerStatus(state="CONTINUE"), base_dir=tmp_path)


def test_read_status_best_effort(tmp_path) -> None:
    # Missing file -> None.
    assert read_status(tmp_path / "nope" / "status.json") is None
    assert read_status_for("ghost", tmp_path) is None

    # Corrupt JSON -> None, no raise.
    bad = tmp_path / "bad" / "status.json"
    bad.parent.mkdir(parents=True)
    bad.write_text("{ not json", encoding="utf-8")
    assert read_status(bad) is None

    # Non-object JSON -> None.
    arr = tmp_path / "arr" / "status.json"
    arr.parent.mkdir(parents=True)
    arr.write_text("[1, 2, 3]", encoding="utf-8")
    assert read_status(arr) is None


# ---------------------------------------------------------------------------
# read_statuses_for_agents — name -> fleet key mapping
# ---------------------------------------------------------------------------


def test_read_statuses_for_agents_keys_by_agent_key(tmp_path) -> None:
    write_status(ManagerStatus(state="HUMAN_GATE", name="alpha"), base_dir=tmp_path)
    # bravo has no status file; an unnamed agent is skipped entirely.
    agents = [
        _bg("aaaa1111", "alpha"),
        _bg("bbbb2222", "bravo"),
        AgentInfo.from_dict({"sessionId": "sess-x", "kind": "interactive"}),
    ]
    statuses = read_statuses_for_agents(agents, tmp_path)
    assert set(statuses) == {"aaaa1111"}  # keyed by short id, matched via name
    assert statuses["aaaa1111"].state == "HUMAN_GATE"


# ---------------------------------------------------------------------------
# Merge into ManagerSnapshot / FleetState
# ---------------------------------------------------------------------------


def test_snapshot_merges_report_into_attention_and_headline() -> None:
    gate = ManagerStatus(state="HUMAN_GATE", summary="needs the robot")
    snap = ManagerSnapshot.from_agent_info(_bg("a", "alpha", state="working"), report=gate)
    # Roster says "working", but the emitted report escalates attention.
    assert snap.needs_attention
    assert snap.headline == "needs the robot"

    # Headline falls back to the transcript tail when there's no report summary.
    plain = ManagerSnapshot.from_agent_info(_bg("a", "alpha"), transcript=["last log"])
    assert plain.headline == "last log"


def test_fleet_state_apply_merges_statuses() -> None:
    fs = FleetState()
    snap = fs.apply(
        [_bg("aaaa1111", "alpha", state="working")],
        statuses={"aaaa1111": ManagerStatus(state="HUMAN_GATE", summary="gate!")},
        now=1.0,
    )
    m = snap.get("alpha")
    assert m.report.state == "HUMAN_GATE"
    assert m.needs_attention
    assert [x.id for x in snap.attention] == ["aaaa1111"]


def test_fleet_state_status_change_fires_subscribers() -> None:
    fs = FleetState()
    seen = []
    fs.subscribe(seen.append)
    agent = _bg("aaaa1111", "alpha")
    fs.apply([agent], statuses={"aaaa1111": ManagerStatus(state="CONTINUE", updated_at=1.0)})
    # Same content, new timestamp only -> NOT a change (updated_at compare=False).
    fs.apply([agent], statuses={"aaaa1111": ManagerStatus(state="CONTINUE", updated_at=2.0)})
    # Real state change -> fires.
    fs.apply([agent], statuses={"aaaa1111": ManagerStatus(state="HUMAN_GATE", updated_at=3.0)})
    assert len(seen) == 2
    assert seen[-1].get("alpha").report.state == "HUMAN_GATE"


# ---------------------------------------------------------------------------
# FleetPoller — status sources
# ---------------------------------------------------------------------------


def test_poller_reads_statuses_from_dir(tmp_path) -> None:
    write_status(ManagerStatus(state="HUMAN_GATE", summary="gate", name="alpha"), base_dir=tmp_path)
    fs = FleetState()
    poller = FleetPoller(
        fs,
        status_dir=str(tmp_path),
        agents_source=lambda: [_bg("aaaa1111", "alpha")],
    )
    snap = poller.poll_once()
    assert snap.get("alpha").report.state == "HUMAN_GATE"
    assert snap.get("alpha").needs_attention


def test_poller_statuses_source_overrides_dir() -> None:
    fs = FleetState()
    poller = FleetPoller(
        fs,
        agents_source=lambda: [_bg("aaaa1111", "alpha")],
        statuses_source=lambda agents: {"aaaa1111": ManagerStatus(state="COMPLETE")},
    )
    snap = poller.poll_once()
    assert snap.get("alpha").report.state == "COMPLETE"
    assert snap.get("alpha").report.is_terminal


def test_poller_no_status_config_means_no_report() -> None:
    fs = FleetState()
    poller = FleetPoller(fs, agents_source=lambda: [_bg("aaaa1111", "alpha")])
    snap = poller.poll_once()
    assert snap.get("alpha").report is None


def test_poller_derives_status_from_transcript_fallback() -> None:
    """With no status.json, the poller derives a status from the sentinel in logs (U13)."""
    fs = FleetState()
    poller = FleetPoller(
        fs,
        agents_source=lambda: [_bg("aaaa1111", "alpha")],
        logs_source=lambda key: ["working...", "ROADMAP_STATE: HUMAN_GATE"],
        fetch_logs=True,
        derive_status_from_transcript=True,
    )
    snap = poller.poll_once()
    m = snap.get("alpha")
    assert m.report is not None and m.report.state == "HUMAN_GATE"
    assert m.needs_attention


def test_poller_status_json_wins_over_transcript() -> None:
    """An emitted status.json always beats the transcript-derived fallback (U13)."""
    fs = FleetState()
    poller = FleetPoller(
        fs,
        agents_source=lambda: [_bg("aaaa1111", "alpha")],
        logs_source=lambda key: ["ROADMAP_STATE: FAILED"],
        fetch_logs=True,
        derive_status_from_transcript=True,
        statuses_source=lambda agents: {"aaaa1111": ManagerStatus(state="CONTINUE")},
    )
    snap = poller.poll_once()
    assert snap.get("alpha").report.state == "CONTINUE"


def test_poller_no_fallback_without_flag() -> None:
    """The transcript fallback is OFF by default (existing behaviour unchanged)."""
    fs = FleetState()
    poller = FleetPoller(
        fs,
        agents_source=lambda: [_bg("aaaa1111", "alpha")],
        logs_source=lambda key: ["ROADMAP_STATE: HUMAN_GATE"],
        fetch_logs=True,
    )
    snap = poller.poll_once()
    assert snap.get("alpha").report is None
