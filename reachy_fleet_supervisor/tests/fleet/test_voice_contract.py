"""Tests for the U17 voice interaction contract (confirm-before-kickoff)."""

from __future__ import annotations

from reachy_fleet_supervisor.fleet.voice_contract import (
    EXPLAIN_OPTIONS_TEXT,
    sounds_like_confirmation,
    summarize_spawn_confirmation,
    wants_immediate_kickoff,
)


# ---------------------------------------------------------------------------
# wants_immediate_kickoff
# ---------------------------------------------------------------------------


def test_wants_immediate_kickoff_matches_common_phrases():
    assert wants_immediate_kickoff("just do it")
    assert wants_immediate_kickoff("yeah just do it please")
    assert wants_immediate_kickoff("just spawn it on that project")
    assert wants_immediate_kickoff("no need to confirm, go")
    assert wants_immediate_kickoff("Skip The Confirmation")


def test_wants_immediate_kickoff_false_for_normal_request():
    assert not wants_immediate_kickoff("spawn a manager called tester on the fleet project")
    assert not wants_immediate_kickoff(None)
    assert not wants_immediate_kickoff("")


# ---------------------------------------------------------------------------
# sounds_like_confirmation
# ---------------------------------------------------------------------------


def test_sounds_like_confirmation_matches_yes_variants():
    for phrase in ["go ahead", "yes", "yep", "sounds good", "do it", "that's right", "kick it off"]:
        assert sounds_like_confirmation(phrase), phrase


def test_sounds_like_confirmation_false_for_correction():
    assert not sounds_like_confirmation("no, use the other project")
    assert not sounds_like_confirmation(None)
    assert not sounds_like_confirmation("")


# ---------------------------------------------------------------------------
# summarize_spawn_confirmation
# ---------------------------------------------------------------------------


def test_summarize_spawn_confirmation_default_case_is_short():
    text = summarize_spawn_confirmation(
        name="tester",
        task="add unit tests",
        cwd="/home/user/projects/fleet",
    )
    assert "tester" in text
    assert "fleet" in text
    assert "add unit tests" in text
    assert "go ahead" in text.lower()
    # Defaults shouldn't be called out.
    assert "run mode" not in text.lower()
    assert "permission mode" not in text.lower()


def test_summarize_spawn_confirmation_calls_out_non_default_run_mode():
    text = summarize_spawn_confirmation(
        name="steerer",
        task="refactor the api",
        cwd="/home/user/projects/fleet",
        run_mode="remote-control",
    )
    assert "remote-control" in text.lower()


def test_summarize_spawn_confirmation_calls_out_non_default_permission_mode():
    text = summarize_spawn_confirmation(
        name="cautious",
        task="update the schema",
        cwd="/home/user/projects/fleet",
        permission_mode="acceptEdits",
    )
    assert "acceptEdits" in text


def test_summarize_spawn_confirmation_includes_gate_note():
    text = summarize_spawn_confirmation(
        name="tester",
        task="add unit tests",
        cwd="/home/user/projects/fleet",
        gate_note="It will stop and ask before risky commands.",
    )
    assert "stop and ask" in text


def test_summarize_spawn_confirmation_strips_trailing_period_from_task():
    text = summarize_spawn_confirmation(
        name="tester",
        task="add unit tests.",
        cwd="/home/user/projects/fleet",
    )
    # The task's own trailing period is stripped so it doesn't double up
    # with the sentence's closing period.
    assert "add unit tests.." not in text
    assert "add unit tests" in text


# ---------------------------------------------------------------------------
# EXPLAIN_OPTIONS_TEXT
# ---------------------------------------------------------------------------


def test_explain_options_text_mentions_run_modes_and_example():
    lowered = EXPLAIN_OPTIONS_TEXT.lower()
    assert "remote-control" in lowered
    assert "background" in lowered
    assert "spawn a manager called" in lowered
    assert "just do it" in lowered
