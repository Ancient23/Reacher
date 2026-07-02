"""Fleet skill (U25, decision #18) — a `drive-loop` variant installed into
manager sessions.

Covers:

- :func:`build_fleet_skill_markdown` — the pure content builder (frontmatter +
  conventions: orchestrate via subagents/Workflows, short spoken replies,
  escalate HUMAN_GATE through the status file).
- :func:`write_fleet_skill_file` / :func:`fleet_skill_path` — materialization
  under ``<repo>/.claude/skills/<name>/SKILL.md``, including the
  overwrite-by-default vs. preserve-existing behavior.
- :func:`spawn_drive_manager` wiring: the skill is installed before spawn (by
  default) and the built task prompt references it; ``install_skill=False``
  opts out of both.

All headless — no subprocess, no robot (this skill doubles as the fleet's own
headless dev/test harness per decision #18).
"""

from __future__ import annotations

from reachy_fleet_supervisor.fleet import (
    FLEET_SKILL_NAME,
    DriveLoopSpec,
    build_drive_task,
    build_fleet_skill_markdown,
    fleet_skill_path,
    spawn_drive_manager,
    write_fleet_skill_file,
)


# ---------------------------------------------------------------------------
# build_fleet_skill_markdown — pure content builder
# ---------------------------------------------------------------------------


def test_build_fleet_skill_markdown_has_frontmatter() -> None:
    md = build_fleet_skill_markdown(status_path="/tmp/status.json")
    assert md.startswith("---\n")
    assert f"name: {FLEET_SKILL_NAME}" in md
    assert "description:" in md


def test_build_fleet_skill_markdown_encodes_conventions() -> None:
    md = build_fleet_skill_markdown(status_path="/tmp/status.json", sentinel_prefix="ROADMAP_STATE")
    # (1) orchestrate via subagents/Workflows
    assert "subagent" in md.lower()
    assert "Workflow" in md
    # (2) keep spoken replies short
    assert "short" in md.lower()
    assert '"summary"' in md
    # (3) escalate HUMAN_GATE by voice, via the status file
    assert "HUMAN_GATE" in md
    assert "/tmp/status.json" in md
    assert "ROADMAP_STATE" in md


def test_build_fleet_skill_markdown_vision_clause_optional() -> None:
    without = build_fleet_skill_markdown(status_path="/s.json")
    assert "vision tool" not in without.lower()

    with_vision = build_fleet_skill_markdown(
        status_path="/s.json", vision_command="uv run --project . fleet look"
    )
    assert "uv run --project . fleet look" in with_vision
    assert "vision tool" in with_vision.lower()


# ---------------------------------------------------------------------------
# fleet_skill_path / write_fleet_skill_file — materialization
# ---------------------------------------------------------------------------


def test_fleet_skill_path(tmp_path) -> None:
    p = fleet_skill_path(tmp_path / "repo")
    assert p == tmp_path / "repo" / ".claude" / "skills" / FLEET_SKILL_NAME / "SKILL.md"


def test_write_fleet_skill_file_creates_and_overwrites(tmp_path) -> None:
    spec = DriveLoopSpec(name="skilled", repo=tmp_path / "repo", status_dir=tmp_path / "s")
    path = write_fleet_skill_file(spec)
    assert path == fleet_skill_path(spec.repo)
    assert path.is_file()
    content = path.read_text(encoding="utf-8")
    assert str(spec.status_path()) in content

    # Overwrites by default (supervisor-authored content, not manager output).
    path.write_text("stale", encoding="utf-8")
    write_fleet_skill_file(spec)
    assert path.read_text(encoding="utf-8") != "stale"


def test_write_fleet_skill_file_preserves_when_overwrite_false(tmp_path) -> None:
    spec = DriveLoopSpec(name="skilled2", repo=tmp_path / "repo", status_dir=tmp_path / "s")
    path = write_fleet_skill_file(spec)
    path.write_text("human-edited", encoding="utf-8")
    write_fleet_skill_file(spec, overwrite=False)
    assert path.read_text(encoding="utf-8") == "human-edited"


# ---------------------------------------------------------------------------
# spawn_drive_manager wiring
# ---------------------------------------------------------------------------


class _FakeSession:
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


def test_spawn_drive_manager_installs_skill_by_default(tmp_path) -> None:
    spec = DriveLoopSpec(name="driver3", repo=tmp_path / "repo", status_dir=tmp_path / "s")
    fake = _FakeSession()
    spawn_drive_manager(fake, spec)

    skill_path = fleet_skill_path(spec.repo)
    assert skill_path.is_file()
    assert f".claude/skills/{FLEET_SKILL_NAME}/SKILL.md".replace("/", "\\") in fake.calls[0][
        "task"
    ].replace("/", "\\") or f".claude/skills/{FLEET_SKILL_NAME}/SKILL.md" in fake.calls[0]["task"]


def test_build_drive_task_skill_clause_present_by_default(tmp_path) -> None:
    spec = DriveLoopSpec(name="d4", repo=tmp_path / "repo", status_dir=tmp_path / "s")
    task = build_drive_task(spec)
    assert "FLEET SKILL" in task
    assert FLEET_SKILL_NAME in task


def test_spawn_drive_manager_install_skill_false_opts_out(tmp_path) -> None:
    spec = DriveLoopSpec(
        name="driver5",
        repo=tmp_path / "repo",
        status_dir=tmp_path / "s",
        install_skill=False,
    )
    fake = _FakeSession()
    spawn_drive_manager(fake, spec)

    skill_path = fleet_skill_path(spec.repo)
    assert not skill_path.exists()
    assert "FLEET SKILL" not in fake.calls[0]["task"]
