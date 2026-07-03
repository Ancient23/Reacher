"""Vision — the proactive **supervisor observer** (U21, decision #9).

U20 gave a *worker* a "look at what I built" tool it calls itself. Decision #9
also asks for the other direction: Reachy, as the *supervisor*, proactively
**glances and comments** on fleet work without being asked — the natural
moment being "a manager just finished its task." This module is that half.

Mirrors :mod:`.voice` exactly (one more edge-triggered :class:`FleetState`
subscriber):

- **Pure mapping** (unit-tested, no I/O): :func:`should_glance` decides which
  :class:`~reachy_fleet_supervisor.fleet.state.ManagerSnapshot` transitions are
  worth a glance (default: completion only — matches the human framing "Reachy
  glances at what was built"), and :func:`build_glance_question` renders the
  vision question a glance asks (pure string, references the manager by name).
- **The FleetState subscriber**: :class:`FleetObserver` tracks each manager's
  last-glanced kind (via :mod:`.voice`'s ``speakable_kind``) and, on a fresh
  transition into a glance-worthy kind, calls an injected ``look_fn`` (defaults
  to :func:`~reachy_fleet_supervisor.fleet.vision.look`) and speaks a short
  comment built from the assessment.

Two capture sources, same split as U20:

- ``screen`` (default) — digital work; fully software-testable end to end
  against a fake ``look_fn``/subprocess.
- ``camera`` — physical work via the robot's camera; that path is
  hardware-gated at the :mod:`.vision` layer (``RobotCameraUnavailable``
  without a wired frame provider). :class:`FleetObserver` treats that
  exception as a normal "nothing to say this time" — it logs and moves on,
  never faking a perceptual comment. Actually seeing Reachy glance + comment
  out loud can only be confirmed on the physical robot (see
  ``docs/roadmap/STATE.yaml`` ``status.deferred_gates``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from .state import FleetState, FleetSnapshot, ManagerSnapshot
from .config import GatePolicy
from .voice import VOICE_COMPLETED, SpeakFn, _label, speakable_kind
from .vision import SOURCE_SCREEN, LookResult, VisionError, look as vision_look

logger = logging.getLogger(__name__)

#: A callable with :func:`~reachy_fleet_supervisor.fleet.vision.look`'s
#: signature (injectable for tests; the module-level default is the real tool).
LookFn = Callable[..., LookResult]

#: Which voice kinds warrant a proactive glance. Completion only, by default —
#: "Reachy glances at what was just built," not at every failure/gate (those
#: are already spoken by :class:`~reachy_fleet_supervisor.fleet.voice.FleetVoiceRenderer`
#: without needing a visual assessment).
DEFAULT_GLANCE_KINDS = frozenset({VOICE_COMPLETED})


def should_glance(
    m: ManagerSnapshot,
    *,
    policy: Optional[GatePolicy] = None,
    glance_kinds: frozenset[str] = DEFAULT_GLANCE_KINDS,
) -> bool:
    """Is manager *m*'s current state worth a proactive glance? (pure)

    Reuses :func:`~reachy_fleet_supervisor.fleet.voice.speakable_kind` so the
    observer and the voice renderer agree on what a manager's state IS; it
    just reacts to a narrower set of kinds (completion by default).
    """
    kind = speakable_kind(m, policy=policy)
    return kind is not None and kind in glance_kinds


def build_glance_question(m: ManagerSnapshot) -> str:
    """The vision question a glance asks, referencing *m* by name/cwd (pure)."""
    label = _label(m)
    where = f" in {m.cwd}" if m.cwd else ""
    return (
        f"{label} just finished a task{where}. Take a quick look at what it "
        f"built and give a one-sentence verdict a supervisor could say out "
        f"loud — does it look right, and is anything obviously off?"
    )


def build_glance_comment(m: ManagerSnapshot, assessment: str) -> str:
    """Turn a raw vision assessment into a short spoken comment (pure).

    Kept terse (front-loads the manager's name, like :mod:`.voice`'s phrases)
    and single-line — a long multi-paragraph assessment is trimmed to its
    first sentence/line so it stays speakable.
    """
    label = _label(m)
    text = (assessment or "").strip()
    if not text:
        return f"I glanced at {label}'s work but didn't have anything to say."
    first_line = text.splitlines()[0].strip()
    return f"I took a look at {label}'s work — {first_line}"


@dataclass(frozen=True)
class GlanceReport:
    """One proactive glance the observer took (key, question, comment said)."""

    key: str
    question: str
    comment: str


class FleetObserver:
    """Proactively glance + comment on fleet work: a FleetState subscriber (U21).

    Edge-triggered exactly like :class:`~reachy_fleet_supervisor.fleet.voice.FleetVoiceRenderer`:
    a manager is glanced at only on a FRESH transition into a glance-worthy
    kind (default: completion), never repeatedly while it stays in that state.
    Primes silently on the first snapshot by default so a restart that adopts
    already-finished managers doesn't retroactively glance at all of them.

    ``look_fn`` and ``speak`` are both injected — the live wiring passes
    :func:`~reachy_fleet_supervisor.fleet.vision.look` (screen source) and
    :meth:`OpenaiRealtimeHandler.speak_threadsafe`; tests inject fakes. A
    failing glance (vision error, e.g. no screen / no camera / claude CLI
    hiccup) is logged and swallowed — a bad glance must never break the
    poller, same contract as the voice/body renderers.
    """

    def __init__(
        self,
        speak: SpeakFn,
        *,
        look_fn: Optional[LookFn] = None,
        source: str = SOURCE_SCREEN,
        policy: Optional[GatePolicy] = None,
        glance_kinds: frozenset[str] = DEFAULT_GLANCE_KINDS,
        announce_initial: bool = False,
    ) -> None:
        self._speak = speak
        self._look_fn = look_fn or vision_look
        self._source = source
        self._policy = policy or GatePolicy()
        self._glance_kinds = glance_kinds
        self._announce_initial = announce_initial
        self._last_kinds: dict[str, Optional[str]] = {}
        self._primed = False

    @property
    def last_kinds(self) -> dict[str, Optional[str]]:
        """The last-seen kind per manager key (copy; for tests/inspection)."""
        return dict(self._last_kinds)

    def _glance(self, m: ManagerSnapshot) -> Optional[GlanceReport]:
        question = build_glance_question(m)
        logger.info(
            "fleet observer: glance attempted for %s (source=%s)", m.key, self._source
        )
        try:
            result = self._look_fn(question, source=self._source)
        except VisionError:
            # Includes RobotCameraUnavailable — the camera path is
            # hardware-gated; a missing camera just means "nothing to glance
            # at this time," never a faked comment.
            logger.info("fleet observer: glance unavailable for %s", m.key, exc_info=True)
            return None
        except Exception:  # noqa: BLE001 — one bad glance must not break the poller
            logger.exception("fleet observer: glance failed for %s", m.key)
            return None
        comment = build_glance_comment(m, result.assessment)
        return GlanceReport(key=m.key, question=question, comment=comment)

    def _glances(self, snapshot: FleetSnapshot) -> list[GlanceReport]:
        speak_baseline = self._primed or self._announce_initial
        reports: list[GlanceReport] = []
        seen: dict[str, Optional[str]] = {}
        for m in snapshot.managers:
            kind = speakable_kind(m, policy=self._policy)
            seen[m.key] = kind
            previous = self._last_kinds.get(m.key)
            worth_it = kind is not None and kind in self._glance_kinds
            if worth_it and kind != previous and speak_baseline:
                report = self._glance(m)
                if report is not None:
                    reports.append(report)
        self._last_kinds = seen
        self._primed = True
        return reports

    def on_snapshot(self, snapshot: FleetSnapshot) -> list[GlanceReport]:
        """Compute + speak glances for *snapshot*; return what was said."""
        reports = self._glances(snapshot)
        for report in reports:
            logger.info("fleet observer: speaking glance for %s", report.key)
            try:
                self._speak(report.comment)
            except Exception:  # noqa: BLE001 — a speech hiccup must not kill the poller
                logger.exception("fleet observer: speak failed for %r", report.comment)
                continue
            logger.info("observer → [%s] %s", report.key, report.comment)
        return reports

    def attach(
        self, state: FleetState, *, fire_immediately: bool = True
    ) -> Callable[[], None]:
        """Subscribe to *state*; return a zero-arg unsubscribe handle."""

        def _sub(snapshot: FleetSnapshot) -> None:
            self.on_snapshot(snapshot)

        return state.subscribe(_sub, fire_immediately=fire_immediately)
