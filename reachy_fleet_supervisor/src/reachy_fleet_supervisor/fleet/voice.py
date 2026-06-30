"""Voice renders FleetState — the robot's *speech* is a renderer too (U15).

The fleet's design is **one source of truth, many renderers** (plan.md §3): the
headless CLI, the web dashboard and the robot **body** (:mod:`.body`) all
subscribe to a single :class:`~reachy_fleet_supervisor.fleet.state.FleetState`.
This module adds the robot's **voice** as a fourth renderer — the complement of
the body. Where the body *signals* who needs attention with yaw + antennas
(continuous, ambient), the voice *says it out loud* on the moments that matter
(discrete, edge-triggered): a manager **failed** ("build failed, needs you"), a
manager **gated** for a human ("tests need you"), or a manager **completed** its
task ("the tester finished"). Decision #17: the ralph loop's ``HUMAN_GATE``
sentinel IS the escalation channel — Reachy speaks the gate; the spoken answer
resumes the loop (the resume seam is :class:`FleetController` / the ``answer_gate``
voice tool).

Two layers, mirroring :mod:`.body`:

- **Pure mapping** (unit-tested, no audio): :func:`speakable_kind` classifies one
  :class:`~reachy_fleet_supervisor.fleet.state.ManagerSnapshot` into an optional
  :data:`VoiceKind` (``failed`` / ``gate`` / ``completed`` / ``None``), reusing
  U14's :func:`~reachy_fleet_supervisor.fleet.config.classify_status` +
  :class:`~reachy_fleet_supervisor.fleet.config.GatePolicy` so an *autonomous*
  gate (auto-approvable) stays silent while an *escalating* one speaks.
  :func:`voice_phrase_for` turns a kind + manager into a short spoken line.
- **The FleetState subscriber**: :class:`FleetVoiceRenderer` tracks each manager's
  last-announced kind and speaks **only on a transition** (so a manager that stays
  failed isn't announced every poll). The *speak* callback is injected — the live
  wiring routes it to the OpenAI Realtime host
  (``OpenaiRealtimeHandler.speak_threadsafe``, which makes Reachy actually talk);
  tests inject a recording fake. Because real speech can only be confirmed on the
  physical robot, U15 is a HARDWARE gate: this module is software-tested end to
  end against a fake speak callback, but "Reachy speaks it" is verified by a human.

The voice renderer hooks the **same FleetState transitions the body already reacts
to** — body + voice are two renderers of one state, not two pipelines.
"""

from __future__ import annotations
import logging
from typing import Callable, Optional, Sequence
from dataclasses import dataclass

from .state import FleetState, FleetSnapshot, ManagerSnapshot
from .config import (
    GatePolicy,
    GATE_ESCALATE,
    classify_status,
)
from .status import (
    SENTINEL_FAILED,
    SENTINEL_COMPLETE,
    SENTINEL_HUMAN_GATE,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Speakable kinds (the three moments worth saying out loud)
# ---------------------------------------------------------------------------

# A manager hit a hard failure (build/test failed) — always spoken.
VOICE_FAILED = "failed"
# A manager raised a HUMAN_GATE the policy escalates — spoken ("needs you").
VOICE_GATE = "gate"
# A manager finished its task (COMPLETE) — spoken (the human stressed: speak on
# completion, not just on gate escalations).
VOICE_COMPLETED = "completed"

# The kind of a speakable event, or ``None`` when nothing is worth saying.
VoiceKind = str

# A speak callback: hand it a phrase and it makes the robot say it (best-effort).
SpeakFn = Callable[[str], None]


def _label(m: ManagerSnapshot) -> str:
    """The spoken handle for a manager: its name, falling back to its key."""
    return m.name or m.key


def speakable_kind(
    m: ManagerSnapshot, *, policy: Optional[GatePolicy] = None
) -> Optional[VoiceKind]:
    """Classify one manager into a speakable :data:`VoiceKind`, or ``None``.

    Prefers the manager's own emitted report (decision #21 — its drive-loop
    sentinel) and falls back to the ``claude agents --json`` roster row, exactly
    like :func:`~reachy_fleet_supervisor.fleet.body.signal_state`:

    - ``FAILED`` (report sentinel or roster ``state``) → :data:`VOICE_FAILED`.
    - ``HUMAN_GATE`` → :data:`VOICE_GATE` **only if** the gate *escalates* under
      *policy* (U14 :func:`classify_status`); an autonomous/auto-approvable gate
      returns ``None`` so the voice does not interrupt the human for it.
    - ``COMPLETE`` (report sentinel or roster ``state``) → :data:`VOICE_COMPLETED`.
    - anything else (running / continue / idle) → ``None``.

    The voice deliberately speaks *fewer* states than the body shows: the body is
    ambient (it can sit on "working"), the voice only interjects on the three
    moments a human actually wants spoken.
    """
    policy = policy or GatePolicy()
    report = m.report
    state = report.normalized_state if report is not None else None
    raw_state = (m.state or "").strip().lower()

    if state == SENTINEL_FAILED or raw_state == "failed":
        return VOICE_FAILED
    if state == SENTINEL_HUMAN_GATE:
        # Reuse U14's gate policy: only an escalating gate is spoken.
        if classify_status(policy, report) == GATE_ESCALATE:  # type: ignore[arg-type]
            return VOICE_GATE
        return None
    if state == SENTINEL_COMPLETE or raw_state in ("done", "complete", "completed"):
        return VOICE_COMPLETED
    return None


def _gate_detail(m: ManagerSnapshot) -> Optional[str]:
    """A short reason to tack onto a gate/failure phrase, if the manager gave one.

    Prefers the manager's HUMAN_GATE ``gate`` text, then its one-line ``summary``;
    trimmed to a single short clause so the spoken line stays brief.
    """
    report = m.report
    if report is None:
        return None
    for candidate in (report.gate, report.summary):
        if candidate and candidate.strip():
            # Keep it to the first line; gate steps can be multi-line copy-paste.
            first = candidate.strip().splitlines()[0].strip()
            if first:
                return first
    return None


def voice_phrase_for(kind: VoiceKind, m: ManagerSnapshot) -> str:
    """Render a short spoken line for a speakable *kind* on manager *m* (pure).

    Phrases are deliberately terse and front-load the manager's name so the human
    immediately knows *who*: e.g. ``"tester failed the build and needs you."`` or
    ``"tester finished its task."`` A reason clause (from the gate/summary) is
    appended when the manager provided one.
    """
    label = _label(m)
    detail = _gate_detail(m)
    if kind == VOICE_FAILED:
        base = f"Heads up — {label} failed and needs you"
    elif kind == VOICE_GATE:
        base = f"{label} needs you"
    elif kind == VOICE_COMPLETED:
        base = f"{label} finished its task"
    else:  # pragma: no cover - defensive; callers pass a real kind
        base = f"{label} has an update"
    if detail and kind in (VOICE_FAILED, VOICE_GATE):
        return f"{base}: {detail}"
    return f"{base}."


# ---------------------------------------------------------------------------
# FleetVoiceRenderer — the FleetState subscriber (edge-triggered)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VoiceAnnouncement:
    """One thing the voice renderer decided to say (the key, kind and phrase)."""

    key: str
    kind: VoiceKind
    phrase: str


class FleetVoiceRenderer:
    """Speak fleet state changes aloud: a FleetState subscriber (U15).

    Each fleet update is reduced to a per-manager :data:`VoiceKind`; a phrase is
    spoken **only when a manager's kind changes** (so a manager that stays
    ``failed`` is announced once, not every poll). The *speak* callback is
    injected — :meth:`OpenaiRealtimeHandler.speak_threadsafe` on the live robot, a
    recording fake in tests — so the mapping is testable without any audio stack.

    First-snapshot policy: by default the renderer **primes silently** on the
    first snapshot (records each manager's current kind without speaking), so a
    restart that adopts already-finished/already-gated managers doesn't blurt a
    backlog of stale announcements. Pass ``announce_initial=True`` to speak the
    baseline too.
    """

    def __init__(
        self,
        speak: SpeakFn,
        *,
        policy: Optional[GatePolicy] = None,
        announce_initial: bool = False,
    ) -> None:
        """Build a renderer that says phrases via *speak*.

        *policy* is the gate policy used to decide whether a HUMAN_GATE escalates
        to speech (defaults to a plain autonomous policy, under which a *bare*
        gate still escalates — see :func:`classify_status`). With
        *announce_initial* the very first snapshot is spoken too; otherwise it
        only establishes the baseline.
        """
        self._speak = speak
        self._policy = policy or GatePolicy()
        self._announce_initial = announce_initial
        self._last_kinds: dict[str, Optional[VoiceKind]] = {}
        self._primed = False

    @property
    def last_kinds(self) -> dict[str, Optional[VoiceKind]]:
        """The last-announced kind per manager key (copy; for tests/inspection)."""
        return dict(self._last_kinds)

    def _phrases(self, snapshot: FleetSnapshot) -> list[VoiceAnnouncement]:
        """Compute the announcements this snapshot warrants (pure; updates state).

        Edge-triggered: emits a manager only when its kind *changed* to a non-None
        kind since last seen. Drops bookkeeping for managers no longer present so a
        re-appearing manager is treated freshly.
        """
        speak_baseline = self._primed or self._announce_initial
        announcements: list[VoiceAnnouncement] = []
        seen: dict[str, Optional[VoiceKind]] = {}
        for m in snapshot.managers:
            kind = speakable_kind(m, policy=self._policy)
            seen[m.key] = kind
            previous = self._last_kinds.get(m.key)
            if kind is not None and kind != previous and speak_baseline:
                announcements.append(
                    VoiceAnnouncement(m.key, kind, voice_phrase_for(kind, m))
                )
        self._last_kinds = seen
        self._primed = True
        return announcements

    def on_snapshot(self, snapshot: FleetSnapshot) -> list[VoiceAnnouncement]:
        """Compute + speak the announcements for *snapshot*; return what was said.

        A failing *speak* callback is logged, never raised, so one bad utterance
        never breaks the pub/sub fan-out (the body renderer keeps the same
        contract for the motor seam).
        """
        announcements = self._phrases(snapshot)
        for ann in announcements:
            try:
                self._speak(ann.phrase)
            except Exception:  # noqa: BLE001 — a speech hiccup must not kill the poller
                logger.exception("fleet voice: speak failed for %r", ann.phrase)
                continue
            logger.info("voice → [%s] %s", ann.kind, ann.phrase)
        return announcements

    def attach(
        self, state: FleetState, *, fire_immediately: bool = True
    ) -> Callable[[], None]:
        """Subscribe to *state*; return a zero-arg unsubscribe handle.

        ``fire_immediately`` primes the baseline from the current snapshot on
        attach (silently, unless ``announce_initial``), so the first *real* change
        after attach is what gets spoken.
        """

        def _sub(snapshot: FleetSnapshot) -> None:
            self.on_snapshot(snapshot)

        return state.subscribe(_sub, fire_immediately=fire_immediately)
