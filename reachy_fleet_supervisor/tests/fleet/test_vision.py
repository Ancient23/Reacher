"""U20 — vision worker tool: capture (screen | camera) -> Claude assessment.

Every side-effecting seam is injected (grabber / frame provider / subprocess
runner), so the whole software path runs without a screen grab or a real Claude
call. One optional real-screen smoke test grabs the actual desktop when one is
available (skipped headless). The robot-camera path is asserted to be
hardware-GATED (raises rather than pretending to see).
"""

from __future__ import annotations

import json
import io
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from reachy_fleet_supervisor.fleet import cli
from reachy_fleet_supervisor.fleet.drive import DriveLoopSpec, build_drive_task
from reachy_fleet_supervisor.fleet.manager import FleetManagerError
from reachy_fleet_supervisor.fleet import vision
from reachy_fleet_supervisor.fleet.vision import (
    DEFAULT_ASSESS_TIMEOUT,
    JPEG_QUALITY,
    MAX_IMAGE_DIM,
    SOURCE_CAMERA,
    SOURCE_SCREEN,
    VALID_SOURCES,
    LookResult,
    RobotCameraUnavailable,
    VisionError,
    assess_image,
    build_assessment_prompt,
    capture_robot_camera,
    capture_screen,
    default_robot_frame_provider,
    look,
    look_command,
)


def _fake_grabber(w: int = 320, h: int = 200, mode: str = "RGB"):
    def grab():
        return Image.new(mode, (w, h), (30, 60, 90) if mode == "RGB" else None)

    return grab


def _fake_runner(stdout: str = "Looks correct.", returncode: int = 0, stderr: str = ""):
    calls: list[list[str]] = []

    def run(argv, capture_output, text, timeout, check):
        calls.append(list(argv))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    run.calls = calls  # type: ignore[attr-defined]
    return run


# ---------------------------------------------------------------------------
# capture_screen
# ---------------------------------------------------------------------------


class TestCaptureScreen:
    def test_writes_jpeg_and_returns_path(self, tmp_path):
        out = tmp_path / "shots" / "cap.jpg"
        path = capture_screen(out, grabber=_fake_grabber())
        assert path == out and path.is_file()
        with Image.open(path) as img:
            assert img.format == "JPEG"

    def test_temp_file_when_no_out_path(self):
        path = capture_screen(grabber=_fake_grabber())
        try:
            assert path.is_file() and path.suffix == ".jpg"
        finally:
            path.unlink()

    def test_downscales_large_captures(self, tmp_path):
        out = tmp_path / "big.jpg"
        capture_screen(out, grabber=_fake_grabber(4000, 2000))
        with Image.open(out) as img:
            assert max(img.size) <= MAX_IMAGE_DIM

    def test_converts_non_rgb_modes(self, tmp_path):
        out = tmp_path / "rgba.jpg"
        capture_screen(out, grabber=_fake_grabber(mode="RGBA"))
        assert out.is_file()  # JPEG save would fail on RGBA without conversion

    def test_grabber_failure_raises_vision_error(self):
        def boom():
            raise OSError("no display")

        with pytest.raises(VisionError, match="screen capture failed"):
            capture_screen(grabber=boom)

    def test_real_screen_grab_smoke(self, tmp_path):
        """Grab the REAL desktop when one is available (skip headless)."""
        try:
            path = capture_screen(tmp_path / "real.jpg")
        except VisionError as exc:
            pytest.skip(f"no desktop available: {exc}")
        with Image.open(path) as img:
            assert img.size[0] > 0 and max(img.size) <= MAX_IMAGE_DIM


# ---------------------------------------------------------------------------
# capture_robot_camera — hardware-GATED
# ---------------------------------------------------------------------------


class TestCaptureRobotCamera:
    def test_without_frame_provider_is_gated(self):
        with pytest.raises(RobotCameraUnavailable, match="hardware-gated"):
            capture_robot_camera()

    def test_with_injected_pil_frame(self, tmp_path):
        out = tmp_path / "cam.jpg"
        path = capture_robot_camera(out, frame_provider=_fake_grabber())
        assert path == out and out.is_file()

    def test_with_injected_numpy_frame(self, tmp_path):
        np = pytest.importorskip("numpy")
        frame = np.zeros((48, 64, 3), dtype=np.uint8)
        path = capture_robot_camera(tmp_path / "np.jpg", frame_provider=lambda: frame)
        with Image.open(path) as img:
            assert img.size == (64, 48)


# ---------------------------------------------------------------------------
# default_robot_frame_provider — the real Reachy Mini camera wiring (U20
# problem-fix 2026-07-03). Lazy-imports `reachy_mini`; every branch here is
# exercised WITHOUT a real robot/daemon by monkeypatching that import or the
# `ReachyMini` client, so the software suite never needs the hardware.
# ---------------------------------------------------------------------------


class TestDefaultRobotFrameProvider:
    def test_sdk_not_importable_raises_gated(self, monkeypatch):
        """Lazy-import guard: no reachy_mini SDK -> RobotCameraUnavailable, not ImportError."""
        real_import = __import__

        def fake_import(name, *args, **kwargs):
            if name == "reachy_mini":
                raise ImportError("no module named reachy_mini")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", fake_import)
        with pytest.raises(RobotCameraUnavailable, match="not importable"):
            default_robot_frame_provider()

    def test_daemon_unreachable_raises_gated(self, monkeypatch):
        """SDK importable but the daemon connection itself fails -> gated, not a crash."""
        import sys
        import types

        class _BoomReachyMini:
            def __init__(self, *a, **kw):
                raise ConnectionError("no daemon on localhost:8000")

        fake_module = types.ModuleType("reachy_mini")
        fake_module.ReachyMini = _BoomReachyMini
        monkeypatch.setitem(sys.modules, "reachy_mini", fake_module)
        with pytest.raises(RobotCameraUnavailable, match="could not reach"):
            default_robot_frame_provider()

    def test_no_frame_yet_raises_gated(self, monkeypatch):
        """Daemon reachable but returns no frame -> gated, never fakes a frame."""
        import sys
        import types

        class _EmptyFrameReachyMini:
            def __init__(self, *a, **kw):
                self.media = SimpleNamespace(get_frame=lambda: None)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        fake_module = types.ModuleType("reachy_mini")
        fake_module.ReachyMini = _EmptyFrameReachyMini
        monkeypatch.setitem(sys.modules, "reachy_mini", fake_module)
        with pytest.raises(RobotCameraUnavailable, match="no frame yet"):
            default_robot_frame_provider()

    def test_real_frame_is_returned(self, monkeypatch):
        """Happy path: a reachable daemon with a frame -> the raw frame is returned as-is."""
        import sys
        import types

        sentinel_frame = object()

        class _WorkingReachyMini:
            def __init__(self, *a, **kw):
                self.media = SimpleNamespace(get_frame=lambda: sentinel_frame)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        fake_module = types.ModuleType("reachy_mini")
        fake_module.ReachyMini = _WorkingReachyMini
        monkeypatch.setitem(sys.modules, "reachy_mini", fake_module)
        assert default_robot_frame_provider() is sentinel_frame

    def test_wired_end_to_end_through_capture_robot_camera(self, tmp_path, monkeypatch):
        """The provider plugs straight into capture_robot_camera like any other."""
        import sys
        import types

        np = pytest.importorskip("numpy")
        frame = np.full((10, 10, 3), 200, dtype=np.uint8)

        class _WorkingReachyMini:
            def __init__(self, *a, **kw):
                self.media = SimpleNamespace(get_frame=lambda: frame)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        fake_module = types.ModuleType("reachy_mini")
        fake_module.ReachyMini = _WorkingReachyMini
        monkeypatch.setitem(sys.modules, "reachy_mini", fake_module)
        path = capture_robot_camera(
            tmp_path / "robot.jpg", frame_provider=default_robot_frame_provider
        )
        assert path.is_file()


# ---------------------------------------------------------------------------
# assessment (pure prompt + injectable runner)
# ---------------------------------------------------------------------------


class TestAssessImage:
    def test_prompt_names_path_question_and_read(self, tmp_path):
        prompt = build_assessment_prompt(tmp_path / "x.jpg", "is the button blue?")
        assert str(tmp_path / "x.jpg") in prompt
        assert "is the button blue?" in prompt
        assert prompt.startswith("Read ")

    def test_runs_claude_cli_one_shot(self, tmp_path):
        run = _fake_runner("The chart renders correctly.")
        text = assess_image(tmp_path / "x.jpg", "does the chart render?", claude_bin="claude-x", runner=run)
        assert text == "The chart renders correctly."
        argv = run.calls[0]
        assert argv[0] == "claude-x" and argv[1] == "-p"
        assert argv[-2:] == ["--allowedTools", "Read"]
        assert "does the chart render?" in argv[2]

    def test_empty_question_rejected(self, tmp_path):
        with pytest.raises(VisionError, match="question"):
            assess_image(tmp_path / "x.jpg", "  ", runner=_fake_runner())

    def test_nonzero_exit_raises(self, tmp_path):
        run = _fake_runner("", returncode=3, stderr="boom")
        with pytest.raises(VisionError, match="exited 3"):
            assess_image(tmp_path / "x.jpg", "q", runner=run)

    def test_empty_output_raises(self, tmp_path):
        with pytest.raises(VisionError, match="no text"):
            assess_image(tmp_path / "x.jpg", "q", runner=_fake_runner("   "))

    def test_timeout_raises_vision_error(self, tmp_path):
        def run(argv, capture_output, text, timeout, check):
            raise subprocess.TimeoutExpired(argv, timeout)

        with pytest.raises(VisionError, match="timed out"):
            assess_image(tmp_path / "x.jpg", "q", runner=run, timeout=1.5)

    def test_missing_cli_raises_vision_error(self, tmp_path):
        def run(argv, capture_output, text, timeout, check):
            raise FileNotFoundError(argv[0])

        with pytest.raises(VisionError, match="not found"):
            assess_image(tmp_path / "x.jpg", "q", runner=run, claude_bin="nope")


# ---------------------------------------------------------------------------
# look — the tool end-to-end (capture -> assess)
# ---------------------------------------------------------------------------


class TestLook:
    def test_screen_capture_then_assessment(self):
        run = _fake_runner("Verdict: matches the spec.")
        result = look("does it match the spec?", grabber=_fake_grabber(), runner=run)
        assert isinstance(result, LookResult)
        assert result.source == SOURCE_SCREEN
        assert result.assessment == "Verdict: matches the spec."
        assert result.image_path is None  # temp capture deleted after assessment

    def test_temp_capture_deleted_even_on_assess_failure(self):
        captured: list[Path] = []

        def run(argv, capture_output, text, timeout, check):
            # The prompt starts "Read <path> — ..."; record the temp capture path.
            captured.append(Path(argv[2].split(" ")[1]))
            return SimpleNamespace(returncode=1, stdout="", stderr="down")

        with pytest.raises(VisionError):
            look("q", grabber=_fake_grabber(), runner=run)
        assert captured and not captured[0].exists()

    def test_keep_image_retains_capture(self):
        run = _fake_runner()
        result = look("q", grabber=_fake_grabber(), runner=run, keep_image=True)
        assert result.image_path is not None and result.image_path.is_file()
        result.image_path.unlink()

    def test_out_path_is_kept(self, tmp_path):
        out = tmp_path / "kept.jpg"
        result = look("q", grabber=_fake_grabber(), runner=_fake_runner(), out_path=out)
        assert result.image_path == out and out.is_file()

    def test_existing_image_is_assessed_and_kept(self, tmp_path):
        img = tmp_path / "given.jpg"
        Image.new("RGB", (10, 10)).save(img)
        result = look("q", image_path=img, runner=_fake_runner())
        assert result.image_path == img and img.is_file()

    def test_camera_source_is_gated_without_robot(self):
        with pytest.raises(RobotCameraUnavailable):
            look("q", source=SOURCE_CAMERA, runner=_fake_runner())

    def test_camera_source_with_injected_provider(self):
        result = look(
            "q",
            source=SOURCE_CAMERA,
            frame_provider=_fake_grabber(),
            runner=_fake_runner("Physical build looks level."),
        )
        assert result.source == SOURCE_CAMERA
        assert result.assessment == "Physical build looks level."

    def test_invalid_source_rejected(self):
        with pytest.raises(VisionError, match="source"):
            look("q", source="drone", runner=_fake_runner())


# ---------------------------------------------------------------------------
# worker wiring: look_command + drive prompt clause
# ---------------------------------------------------------------------------


class TestWorkerWiring:
    def test_look_command_routes_through_uv_project(self):
        argv = look_command("C:/apps/fleet")
        assert argv[:4] == ["uv", "run", "--project", "C:/apps/fleet"]
        assert argv[4:6] == ["fleet", "look"]
        assert argv[-2:] == ["--source", SOURCE_SCREEN]

    def test_look_command_validates_source(self):
        with pytest.raises(VisionError):
            look_command("p", source="nope")

    def test_drive_prompt_advertises_vision_command(self):
        spec = DriveLoopSpec(name="v", repo="C:/repo", vision_command="uv run --project X fleet look")
        task = build_drive_task(spec)
        assert "VISION TOOL" in task
        assert 'uv run --project X fleet look "<your specific question' in task

    def test_drive_prompt_has_no_vision_clause_by_default(self):
        task = build_drive_task(DriveLoopSpec(name="v", repo="C:/repo"))
        assert "VISION TOOL" not in task

    def test_blank_vision_command_rejected(self):
        with pytest.raises(FleetManagerError, match="vision_command"):
            DriveLoopSpec(name="v", repo="C:/repo", vision_command="   ")


# ---------------------------------------------------------------------------
# CLI: fleet look
# ---------------------------------------------------------------------------


class TestCliLook:
    def _run(self, argv, monkeypatch, fake_look):
        monkeypatch.setattr(cli, "look", fake_look)
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(argv, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def test_look_prints_assessment(self, monkeypatch):
        def fake_look(question, **kw):
            assert question == "is the dashboard readable?"
            assert kw["source"] == SOURCE_SCREEN
            return LookResult(source=SOURCE_SCREEN, image_path=None, assessment="Yes — readable.")

        code, out, _ = self._run(["look", "is the dashboard readable?"], monkeypatch, fake_look)
        assert code == 0 and out.strip() == "Yes — readable."

    def test_look_json_output(self, monkeypatch, tmp_path):
        img = tmp_path / "a.jpg"

        def fake_look(question, **kw):
            return LookResult(source=SOURCE_SCREEN, image_path=img, assessment="ok")

        code, out, _ = self._run(["look", "q", "--json"], monkeypatch, fake_look)
        assert code == 0
        data = json.loads(out)
        assert data == {"source": SOURCE_SCREEN, "image": str(img), "assessment": "ok"}

    def test_look_passes_options_through(self, monkeypatch, tmp_path):
        seen = {}

        def fake_look(question, **kw):
            seen.update(kw)
            return LookResult(source=SOURCE_CAMERA, image_path=None, assessment="x")

        code, _, _ = self._run(
            ["look", "q", "--source", "camera", "--out", str(tmp_path / "o.jpg"),
             "--keep", "--timeout", "33", "--claude-bin", "cbin"],
            monkeypatch,
            fake_look,
        )
        assert code == 0
        assert seen["source"] == SOURCE_CAMERA
        assert seen["out_path"] == str(tmp_path / "o.jpg")
        assert seen["keep_image"] is True
        assert seen["timeout"] == 33.0
        assert seen["claude_bin"] == "cbin"

    def test_look_camera_wires_real_frame_provider_by_default(self, monkeypatch):
        """U20 problem-fix: --source camera must wire the real robot camera
        provider with NO extra flags — this used to always pass frame_provider
        unset, so camera never worked even on a live robot."""
        seen = {}

        def fake_look(question, **kw):
            seen.update(kw)
            return LookResult(source=SOURCE_CAMERA, image_path=None, assessment="x")

        code, _, _ = self._run(["look", "q", "--source", "camera"], monkeypatch, fake_look)
        assert code == 0
        assert seen["frame_provider"] is vision.default_robot_frame_provider

    def test_look_screen_passes_no_frame_provider(self, monkeypatch):
        seen = {}

        def fake_look(question, **kw):
            seen.update(kw)
            return LookResult(source=SOURCE_SCREEN, image_path=None, assessment="x")

        code, _, _ = self._run(["look", "q"], monkeypatch, fake_look)
        assert code == 0
        assert seen["frame_provider"] is None

    def test_look_camera_gated_exit_2(self, monkeypatch):
        def fake_look(question, **kw):
            raise RobotCameraUnavailable("needs the robot")

        code, _, err = self._run(["look", "q", "--source", "camera"], monkeypatch, fake_look)
        assert code == 2 and "needs the robot" in err

    def test_look_vision_error_exit_1(self, monkeypatch):
        def fake_look(question, **kw):
            raise VisionError("claude CLI exited 9")

        code, _, err = self._run(["look", "q"], monkeypatch, fake_look)
        assert code == 1 and "exited 9" in err
