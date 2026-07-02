"""Vision — a worker-callable "look at what I built" tool (U20, decision #9).

A fleet *worker* (a ``claude --bg`` manager) sometimes needs to VERIFY its own
work perceptually — e.g. it built a UI, a chart, or a game level and wants an
assessment of what is actually on screen, not just whether tests pass. This
module gives it that loop:

    capture (screen | robot camera) -> Claude assessment -> text back into the loop

Two capture sources:

- ``screen`` — a digital screenshot via ``PIL.ImageGrab`` (all monitors). Fully
  software-testable; this is the path a worker uses to check digital work.
- ``camera`` — the Reachy Mini's camera for *physical* work. That path needs the
  robot, so it is **hardware-gated**: without an injected ``frame_provider`` it
  raises :class:`RobotCameraUnavailable` rather than pretending to see.

The assessment reuses the ``.phase0/vision_ref`` CLI pattern (Max plan, no API
key): a one-shot ``claude -p <prompt> --allowedTools Read`` subprocess is told to
Read the saved image and answer the worker's question. Every side-effecting seam
(grabber / frame provider / subprocess runner) is injectable so the whole path is
unit-testable without a screen grab or a real Claude call.

How a worker calls it: the ``fleet look "<question>"`` CLI command (see
:mod:`~reachy_fleet_supervisor.fleet.cli`) — a bg manager just runs it via Bash.
:func:`look_command` builds the exact argv (through ``uv run --project`` so it
works from any target repo), and ``DriveLoopSpec.vision_command`` (see
:mod:`~reachy_fleet_supervisor.fleet.drive`) advertises it in a drive manager's
task prompt so the manager knows the tool exists.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence


class VisionError(RuntimeError):
    """A vision capture or assessment failed."""


class RobotCameraUnavailable(VisionError):
    """The robot-camera capture path needs the physical robot (hardware-gated)."""


#: Capture sources a worker can ask for.
SOURCE_SCREEN = "screen"
SOURCE_CAMERA = "camera"
VALID_SOURCES = (SOURCE_SCREEN, SOURCE_CAMERA)

#: Longest edge an image is downscaled to before assessment (keeps the one-shot
#: ``claude -p`` call fast and well under image-size limits).
MAX_IMAGE_DIM = 1568

#: JPEG quality for saved captures.
JPEG_QUALITY = 85

#: Default timeout (s) for the one-shot ``claude -p`` assessment subprocess.
DEFAULT_ASSESS_TIMEOUT = 120.0


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def default_screen_grabber() -> Any:  # pragma: no cover - real screen I/O
    """Grab the full desktop (all monitors) as a PIL image."""
    from PIL import ImageGrab

    return ImageGrab.grab(all_screens=True)


def _save_image(img: Any, out_path: Optional[str | Path], *, max_dim: int = MAX_IMAGE_DIM) -> Path:
    """Downscale *img* to *max_dim* and save it as JPEG; return the path.

    When *out_path* is ``None`` a temp file is created (caller owns cleanup).
    """
    img = img.convert("RGB") if img.mode != "RGB" else img
    img.thumbnail((max_dim, max_dim))
    if out_path is None:
        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.close()
        out_path = tmp.name
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="JPEG", quality=JPEG_QUALITY)
    return path


def capture_screen(
    out_path: Optional[str | Path] = None,
    *,
    grabber: Optional[Callable[[], Any]] = None,
    max_dim: int = MAX_IMAGE_DIM,
) -> Path:
    """Capture the screen to a JPEG file and return its path.

    *grabber* is injectable for tests (must return a PIL image); the default
    grabs the full desktop via ``PIL.ImageGrab``.
    """
    grab = grabber if grabber is not None else default_screen_grabber
    try:
        img = grab()
    except Exception as exc:  # pragma: no cover - depends on desktop session
        raise VisionError(f"screen capture failed: {exc}") from exc
    return _save_image(img, out_path, max_dim=max_dim)


def capture_robot_camera(
    out_path: Optional[str | Path] = None,
    *,
    frame_provider: Optional[Callable[[], Any]] = None,
    max_dim: int = MAX_IMAGE_DIM,
) -> Path:
    """Capture one frame from the robot camera to a JPEG file.

    *frame_provider* must return a numpy ``HxWx3`` RGB array or a PIL image; it
    is the seam the live app wires to the Reachy Mini camera. Without it this
    raises :class:`RobotCameraUnavailable` — the physical-camera path cannot be
    self-verified and is HUMAN-gated (never fake a perceptual check).
    """
    if frame_provider is None:
        raise RobotCameraUnavailable(
            "robot camera capture needs the physical Reachy Mini (no frame provider "
            "wired); use source='screen' for digital work — the camera path is "
            "hardware-gated"
        )
    frame = frame_provider()
    img = frame
    if not hasattr(img, "save"):  # numpy array -> PIL image
        from PIL import Image

        img = Image.fromarray(frame)
    return _save_image(img, out_path, max_dim=max_dim)


# ---------------------------------------------------------------------------
# Assessment (pure prompt + injectable subprocess)
# ---------------------------------------------------------------------------


def build_assessment_prompt(image_path: str | Path, question: str) -> str:
    """Build the one-shot ``claude -p`` prompt that assesses a saved capture."""
    return (
        f"Read {Path(image_path)} — it is a capture of work a coding agent just built "
        f"(a screenshot or a robot-camera photo).\n\n"
        f"You are its visual verifier. Assess the image against this question and answer "
        f"it directly and concretely (what you actually SEE, any problems, and a clear "
        f"verdict). Plain text only, no markdown fences.\n\n"
        f"Question: {question.strip()}"
    )


def assess_image(
    image_path: str | Path,
    question: str,
    *,
    claude_bin: str = "claude",
    timeout: float = DEFAULT_ASSESS_TIMEOUT,
    runner: Optional[Callable[..., Any]] = None,
) -> str:
    """Ask Claude (one-shot CLI, Max plan — no API key) to assess a saved image.

    Runs ``claude -p <prompt> --allowedTools Read`` and returns its stdout text.
    *runner* is injectable for tests (``subprocess.run``-compatible).
    """
    if not str(question).strip():
        raise VisionError("assessment question must not be empty")
    run = runner if runner is not None else subprocess.run
    argv = [claude_bin, "-p", build_assessment_prompt(image_path, question), "--allowedTools", "Read"]
    try:
        result = run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise VisionError(f"claude CLI not found at {claude_bin!r}") from exc
    except subprocess.TimeoutExpired as exc:
        raise VisionError(f"vision assessment timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise VisionError(
            f"claude CLI exited {result.returncode}: {(result.stderr or '').strip()[:400]}"
        )
    text = (result.stdout or "").strip()
    if not text:
        raise VisionError("vision assessment returned no text")
    return text


# ---------------------------------------------------------------------------
# The tool: capture -> assess
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LookResult:
    """One capture + its assessment, handed back into the worker's loop."""

    source: str
    image_path: Optional[Path]
    assessment: str

    def to_dict(self) -> dict[str, object]:
        return {
            "source": self.source,
            "image": str(self.image_path) if self.image_path is not None else None,
            "assessment": self.assessment,
        }


def look(
    question: str,
    *,
    source: str = SOURCE_SCREEN,
    image_path: Optional[str | Path] = None,
    out_path: Optional[str | Path] = None,
    keep_image: bool = False,
    claude_bin: str = "claude",
    timeout: float = DEFAULT_ASSESS_TIMEOUT,
    grabber: Optional[Callable[[], Any]] = None,
    frame_provider: Optional[Callable[[], Any]] = None,
    runner: Optional[Callable[..., Any]] = None,
) -> LookResult:
    """Look at what was built: capture from *source*, assess against *question*.

    - ``image_path`` — assess an EXISTING image instead of capturing.
    - ``out_path`` — save the capture at a specific path (implies keeping it).
    - ``keep_image`` — keep the temp capture file after assessment (default: a
      temp capture is deleted once assessed).
    """
    if source not in VALID_SOURCES:
        raise VisionError(f"source must be one of {VALID_SOURCES}, got {source!r}")
    if image_path is not None:
        captured = Path(image_path)
        owns_temp = False
    else:
        if source == SOURCE_CAMERA:
            captured = capture_robot_camera(out_path, frame_provider=frame_provider)
        else:
            captured = capture_screen(out_path, grabber=grabber)
        owns_temp = out_path is None and not keep_image
    try:
        assessment = assess_image(
            captured, question, claude_bin=claude_bin, timeout=timeout, runner=runner
        )
    finally:
        if owns_temp:
            try:
                captured.unlink()
            except OSError:  # pragma: no cover - best-effort cleanup
                pass
    return LookResult(
        source=source,
        image_path=None if owns_temp else captured,
        assessment=assessment,
    )


def look_command(
    project_dir: str | Path,
    *,
    source: str = SOURCE_SCREEN,
) -> list[str]:
    """Argv PREFIX a worker appends its question to, runnable from any repo.

    Routes through ``uv run --project <fleet app dir>`` so the ``fleet`` console
    script resolves inside the supervisor's venv regardless of the worker's cwd.
    """
    if source not in VALID_SOURCES:
        raise VisionError(f"source must be one of {VALID_SOURCES}, got {source!r}")
    return [
        "uv",
        "run",
        "--project",
        str(project_dir),
        "fleet",
        "look",
        "--source",
        source,
    ]
