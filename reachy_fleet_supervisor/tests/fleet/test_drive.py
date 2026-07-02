"""Managers run ralph/drive loops (U13, decision #15).

Covers the drive-loop seam:

- :func:`build_drive_task` — the pure prompt builder (no subprocess): it names the
  command + repo, bakes in the EXACT status.json path the manager must write, and
  encodes the sentinel vocabulary + the HUMAN_GATE escalation rule.
- :func:`spawn_drive_manager` — the thin spawn wrapper (uses a fake SessionManager
  to assert it spawns the built task with the right name/cwd).
- one REAL end-to-end pass (skipped without the ``claude`` CLI): a real ``claude
  --bg`` manager drives a TRIVIAL sample repo for ONE iteration that hits a
  HUMAN_GATE, writes its status.json, and that sentinel flows into FleetState via
  a real poll — then the session is always torn down (no stray agents).
"""

from __future__ import annotations
import time
import uuid
import shutil
from pathlib import Path

import pytest

from reachy_fleet_supervisor.fleet import (
    FleetState,
    FleetPoller,
    DriveLoopSpec,
    SessionManager,
    build_drive_task,
    spawn_drive_manager,
    status_path_for,
    write_plan_file,
)
from reachy_fleet_supervisor.fleet import state as state_mod
from reachy_fleet_supervisor.fleet.manager import FleetManagerError


_HAS_CLAUDE = shutil.which("claude") is not None


# ---------------------------------------------------------------------------
# DriveLoopSpec validation
# ---------------------------------------------------------------------------


def test_spec_validation(tmp_path) -> None:
    with pytest.raises(FleetManagerError):
        DriveLoopSpec(name="", repo=tmp_path)
    with pytest.raises(FleetManagerError):
        DriveLoopSpec(name="x", repo="")
    with pytest.raises(FleetManagerError):
        DriveLoopSpec(name="x", repo=tmp_path, command="  ")
    with pytest.raises(FleetManagerError):
        DriveLoopSpec(name="x", repo=tmp_path, max_iterations=0)


def test_spec_status_path(tmp_path) -> None:
    spec = DriveLoopSpec(name="alpha", repo=tmp_path / "repo", status_dir=tmp_path / "st")
    assert spec.status_path() == status_path_for("alpha", tmp_path / "st")


# ---------------------------------------------------------------------------
# build_drive_task — pure prompt builder
# ---------------------------------------------------------------------------


def test_build_drive_task_contents(tmp_path) -> None:
    spec = DriveLoopSpec(
        name="builder",
        repo=tmp_path / "myrepo",
        command="/drive-roadmap",
        status_dir=tmp_path / "status",
    )
    task = build_drive_task(spec)
    # Names the command + repo so the manager knows what to loop and where.
    assert "/drive-roadmap" in task
    assert str(tmp_path / "myrepo") in task
    # Bakes in the EXACT status.json path the poller will read (not env-dependent).
    assert str(spec.status_path()) in task
    # Encodes the sentinel vocabulary + the report keys.
    for token in ("CONTINUE", "HUMAN_GATE", "COMPLETE", "FAILED", '"state"', '"gate"'):
        assert token in task
    # The escalation rule: on HUMAN_GATE, STOP (don't loop past a blocker).
    assert "HUMAN_GATE" in task and "STOP" in task
    # Unbounded by default.
    assert "until you reach a HUMAN_GATE" in task


def test_build_drive_task_bounded_and_extra(tmp_path) -> None:
    spec = DriveLoopSpec(
        name="b",
        repo=tmp_path,
        command="/drive-loop",
        max_iterations=1,
        sentinel_prefix="LOOP_STATE",
        status_dir=tmp_path / "s",
        extra_instructions="Be terse.",
    )
    task = build_drive_task(spec)
    assert "at most 1 iteration" in task
    assert "LOOP_STATE: <STATE>" in task
    assert "Be terse." in task


# ---------------------------------------------------------------------------
# written plan file per manager (U16, decision #11)
# ---------------------------------------------------------------------------


def test_spec_plan_defaults_to_none(tmp_path) -> None:
    spec = DriveLoopSpec(name="x", repo=tmp_path)
    assert spec.plan_content is None
    assert spec.resolved_plan_path() == tmp_path / "plan.md"


def test_spec_plan_path_validation(tmp_path) -> None:
    with pytest.raises(FleetManagerError):
        DriveLoopSpec(name="x", repo=tmp_path, plan_path="  ")


def test_resolved_plan_path_relative_and_absolute(tmp_path) -> None:
    rel = DriveLoopSpec(name="x", repo=tmp_path / "repo", plan_path="docs/PLAN.md")
    assert rel.resolved_plan_path() == tmp_path / "repo" / "docs" / "PLAN.md"

    abs_path = tmp_path / "elsewhere" / "PLAN.md"
    absolute = DriveLoopSpec(name="x", repo=tmp_path / "repo", plan_path=abs_path)
    assert absolute.resolved_plan_path() == abs_path


def test_write_plan_file_noop_without_content(tmp_path) -> None:
    spec = DriveLoopSpec(name="x", repo=tmp_path / "repo")
    assert write_plan_file(spec) is None
    assert not (tmp_path / "repo" / "plan.md").exists()


def test_write_plan_file_writes_content(tmp_path) -> None:
    spec = DriveLoopSpec(
        name="x", repo=tmp_path / "repo", plan_content="# Task\nDo the thing.\n"
    )
    written = write_plan_file(spec)
    assert written == tmp_path / "repo" / "plan.md"
    assert written.read_text(encoding="utf-8") == "# Task\nDo the thing.\n"


def test_write_plan_file_does_not_clobber_existing(tmp_path) -> None:
    """A manager's own prior edits (committed state) survive a respawn."""
    spec = DriveLoopSpec(
        name="x", repo=tmp_path / "repo", plan_content="original\n"
    )
    write_plan_file(spec)
    path = spec.resolved_plan_path()
    path.write_text("manager's own edited progress\n", encoding="utf-8")

    # Respawning with the SAME spec (same plan_content) must not overwrite it.
    result = write_plan_file(spec)
    assert result == path
    assert path.read_text(encoding="utf-8") == "manager's own edited progress\n"

    # Explicit overwrite=True is the escape hatch.
    write_plan_file(spec, overwrite=True)
    assert path.read_text(encoding="utf-8") == "original\n"


def test_build_drive_task_includes_plan_clause_when_set(tmp_path) -> None:
    spec = DriveLoopSpec(
        name="planner",
        repo=tmp_path / "repo",
        plan_content="# Plan\ntask + gates\n",
        plan_path="PLAN.md",
    )
    task = build_drive_task(spec)
    assert str(spec.resolved_plan_path()) in task
    assert "WRITTEN PLAN FILE" in task
    assert "authoritative" in task


def test_build_drive_task_omits_plan_clause_by_default(tmp_path) -> None:
    spec = DriveLoopSpec(name="noplanner", repo=tmp_path / "repo")
    task = build_drive_task(spec)
    assert "WRITTEN PLAN FILE" not in task


def test_spawn_drive_manager_materializes_plan_file(tmp_path) -> None:
    spec = DriveLoopSpec(
        name="driver2",
        repo=tmp_path / "repo",
        status_dir=tmp_path / "s",
        plan_content="# Plan\ndo it\n",
    )
    fake = _FakeSession()
    spawn_drive_manager(fake, spec)
    plan_path = spec.resolved_plan_path()
    assert plan_path.is_file()
    assert plan_path.read_text(encoding="utf-8") == "# Plan\ndo it\n"
    # The spawned task references the (now-existing) plan path.
    assert str(plan_path) in fake.calls[0]["task"]


# ---------------------------------------------------------------------------
# spawn_drive_manager — thin wrapper over SessionManager.spawn
# ---------------------------------------------------------------------------


class _FakeSession:
    """Records the spawn call instead of shelling out to claude."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def spawn(self, task, **kwargs):
        self.calls.append({"task": task, **kwargs})

        class _Mgr:
            id = "fake1234"
            name = kwargs["name"]
            session_id = "fake1234-0000"
            cwd = str(kwargs["cwd"])
            run_mode = kwargs.get("run_mode", "background")

        return _Mgr()


def test_spawn_drive_manager_builds_and_spawns(tmp_path) -> None:
    spec = DriveLoopSpec(name="driver", repo=tmp_path / "repo", status_dir=tmp_path / "s")
    fake = _FakeSession()
    mgr = spawn_drive_manager(fake, spec, permission_mode="bypassPermissions")
    assert mgr.name == "driver"
    assert len(fake.calls) == 1
    call = fake.calls[0]
    # The spawned task IS the built drive-loop prompt, with name=spec.name + cwd=repo.
    assert call["task"] == build_drive_task(spec)
    assert call["name"] == "driver"
    assert str(call["cwd"]) == str(tmp_path / "repo")
    assert call["permission_mode"] == "bypassPermissions"


# ---------------------------------------------------------------------------
# REAL end-to-end: a manager drives a trivial sample repo → HUMAN_GATE flows in
# ---------------------------------------------------------------------------


def _make_sample_repo(root: Path) -> Path:
    """A trivial 'repo' with a one-step drive contract that gates on the robot."""
    repo = root / "sample_repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "STATE.txt").write_text(
        "UNIT u1: calibrate the robot's antennas. gate=hardware (needs the physical robot).\n",
        encoding="utf-8",
    )
    (repo / "DRIVE.md").write_text(
        "Drive contract (trivial). Each iteration: read STATE.txt and do the one\n"
        "unit. Unit u1 is gated on the PHYSICAL robot, which is NOT available to\n"
        "you, so you cannot proceed: you MUST escalate. Conclude this iteration\n"
        "with the sentinel line `ROADMAP_STATE: HUMAN_GATE` and the gate text\n"
        "'power on the robot (rear USB-C) and confirm the antennas move'.\n",
        encoding="utf-8",
    )
    return repo


@pytest.mark.skipif(not _HAS_CLAUDE, reason="claude CLI not installed")
def test_drive_loop_human_gate_flows_into_fleet_state(tmp_path) -> None:
    """A real bg manager drives the sample repo to a HUMAN_GATE; it reaches FleetState.

    Bounded + always cleaned up. The manager's single trivial iteration reads two
    small files and writes one status.json, so it finishes well under the budget.
    """
    repo = _make_sample_repo(tmp_path)
    status_dir = tmp_path / "fleet_status"
    name = f"u13-int-{uuid.uuid4().hex[:8]}"
    spec = DriveLoopSpec(
        name=name,
        repo=repo,
        command="follow DRIVE.md",
        max_iterations=1,
        status_dir=status_dir,
    )

    sm = SessionManager()
    mgr = None
    try:
        mgr = spawn_drive_manager(sm, spec)
        assert mgr.id

        # The fleet reads the manager's emitted status.json from status_dir (the
        # decision #21 convention) — scoped to just this session, with the
        # transcript-sentinel fallback on too.
        state = FleetState()
        poller = FleetPoller(
            state,
            predicate=lambda a: a.session_id == mgr.session_id,
            include_all=True,
            fetch_logs=True,
            status_dir=str(status_dir),
            derive_status_from_transcript=True,
        )

        status_file = spec.status_path()
        report = None
        # Bounded wait: poll the fleet until the HUMAN_GATE shows up (status.json
        # OR transcript sentinel). ~12 * 15s = 180s ceiling, never silent.
        for attempt in range(12):
            snap = poller.poll_once()
            m = next((x for x in snap.managers if x.session_id == mgr.session_id), None)
            if m is not None and m.report is not None and m.report.normalized_state == "HUMAN_GATE":
                report = m.report
                break
            if status_file.is_file():  # file landed; one more poll will fold it in
                snap = poller.poll_once()
                m = next((x for x in snap.managers if x.session_id == mgr.session_id), None)
                if m is not None and m.report is not None:
                    report = m.report
                    if report.normalized_state == "HUMAN_GATE":
                        break
            print(f"[u13] waiting for HUMAN_GATE (attempt {attempt + 1}/12)...")
            time.sleep(15)

        assert report is not None, "manager never emitted a status the fleet could read"
        assert report.normalized_state == "HUMAN_GATE", report
        # And it surfaces as needing attention on the snapshot.
        snap = poller.poll_once()
        m = next((x for x in snap.managers if x.session_id == mgr.session_id), None)
        assert m is not None and m.needs_attention
    finally:
        if mgr is not None:
            mgr.stop_and_remove()
        remaining = [
            a for a in state_mod.list_agents(include_all=False) if a.name == name
        ]
        assert remaining == [], f"manager {name} still active after teardown"
