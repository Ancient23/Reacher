"""Reachy fleet supervisor — Phase 1 app (Option A, local-first voice loop).

Architecture (thread-safe by construction):
  * MAIN run() loop (one thread): the ONLY thread that touches the robot. It
    captures mic audio, runs energy VAD to segment utterances, drains the TTS
    playback buffer to the speaker, and drives head/antenna motion from the
    shared status. ~50 Hz.
  * BRAIN thread (asyncio, no robot calls): pulls a finished utterance, runs
    Whisper STT, sends it to the single Claude Code WorkerSession, streams the
    reply (setting status thinking/working), runs Piper TTS, and hands the audio
    back to the main loop via a buffer.

Queues/buffers bridge the two. Built for one worker now; structured so a
SessionManager can hold N later.

Run standalone (headless, needs the daemon up):
    python -m reachy_fleet_supervisor.supervisor_app
"""

from __future__ import annotations

import logging
import math
import os
import queue
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np

from reachy_mini import ReachyMini, ReachyMiniApp
from reachy_mini.utils import create_head_pose

from .claude_brain import WorkerSession, WorkerStatus
from .emotions import EmotionLibrary
from .voice import PiperTTS, WhisperSTT

logger = logging.getLogger(__name__)

# Tunables (env-overridable for portability across machines / the RTX 5090).
WORKER_CWD = os.getenv("FLEET_WORKER_CWD", str(Path(__file__).resolve().parents[3] / ".phase0" / "worker_sandbox"))
VAD_THRESHOLD = float(os.getenv("FLEET_VAD_THRESHOLD", "0.030"))   # RMS on float32 [-1,1]
VAD_SILENCE_S = float(os.getenv("FLEET_VAD_SILENCE_S", "0.6"))     # end-of-utterance hangover
VAD_MIN_S = float(os.getenv("FLEET_VAD_MIN_S", "0.5"))             # ignore blips shorter than this
MOTION_SMOOTH = float(os.getenv("FLEET_MOTION_SMOOTH", "0.10"))    # per-tick ease toward target (lower = smoother)


class ReachyFleetSupervisorApp(ReachyMiniApp):  # type: ignore[misc]
    """Embodied, voice-driven supervisor over a single Claude Code worker (Phase 1)."""

    custom_app_url: Optional[str] = None  # headless for now; Gradio dashboard comes later

    def __init__(self) -> None:
        super().__init__()
        self._stt = WhisperSTT()
        self._tts = PiperTTS()

        self._stt_queue: "queue.Queue[np.ndarray]" = queue.Queue()
        self._play_lock = threading.Lock()
        self._play_buf = np.zeros(0, dtype=np.float32)  # at output sample rate

        self._status_lock = threading.Lock()
        self._status = WorkerStatus.IDLE

        self._in_sr = 16000
        self._out_sr = 16000

        # VAD capture state (main thread only)
        self._in_speech = False
        self._cap: list[np.ndarray] = []
        self._last_voice = 0.0

        # smoothed motion state (degrees) — low-passed toward targets to avoid jolts
        self._mt = {"yaw": 0.0, "pitch": 0.0, "roll": 0.0, "al": 0.0, "ar": 0.0}

        # recorded emotion playback (takes over set_target for the move's duration)
        self._emotions = EmotionLibrary()
        self._emo_lock = threading.Lock()
        self._active_move = None  # tuple(move, start_time) or None

    # ─── shared-status helpers ────────────────────────────────────────────────

    def _set_status(self, status: WorkerStatus) -> None:
        with self._status_lock:
            self._status = status

    def _get_status(self) -> WorkerStatus:
        with self._status_lock:
            return self._status

    def _play_emotion(self, name: str) -> None:
        """Queue a recorded emotion move to take over the body for its duration."""
        move = self._emotions.get(name)
        if move is not None:
            with self._emo_lock:
                self._active_move = (move, time.time())

    def _speaking(self) -> bool:
        with self._play_lock:
            return self._play_buf.shape[0] > 0

    def _play_remaining(self) -> int:
        with self._play_lock:
            return int(self._play_buf.shape[0])

    def _enqueue_playback(self, audio_out_sr: np.ndarray) -> None:
        with self._play_lock:
            self._play_buf = np.concatenate([self._play_buf, audio_out_sr.astype(np.float32)])

    # ─── MAIN loop: mic capture + VAD + playback + motion (robot thread) ──────

    def run(self, reachy_mini: ReachyMini, stop_event: threading.Event) -> None:
        media = reachy_mini.media
        for fn in ("start_recording", "start_playing"):
            try:
                getattr(media, fn)()
            except Exception as exc:  # noqa: BLE001
                logger.warning("media.%s() failed: %s", fn, exc)
        try:
            self._in_sr = int(media.get_input_audio_samplerate())
            self._out_sr = int(media.get_output_audio_samplerate())
        except Exception:  # noqa: BLE001
            pass
        logger.info("Audio: in=%d Hz, out=%d Hz | worker cwd=%s", self._in_sr, self._out_sr, WORKER_CWD)

        brain = threading.Thread(target=self._brain_thread, args=(stop_event,),
                                 daemon=True, name="claude-brain")
        brain.start()

        chunk = max(160, int(self._out_sr * 0.05))  # ~50 ms playback chunks
        t0 = time.time()
        while not stop_event.is_set():
            # 1) capture mic (skip while speaking, to avoid feeding our own TTS back in)
            try:
                sample = media.get_audio_sample()
            except Exception:  # noqa: BLE001
                sample = None
            if sample is not None and len(sample):
                mono = sample.mean(axis=1) if getattr(sample, "ndim", 1) == 2 else np.asarray(sample)
                self._vad_feed(np.asarray(mono, dtype=np.float32))

            # 2) drain TTS playback to the speaker
            with self._play_lock:
                if self._play_buf.shape[0] > 0:
                    out = self._play_buf[:chunk]
                    self._play_buf = self._play_buf[chunk:]
                else:
                    out = None
            if out is not None and out.shape[0] > 0:
                try:
                    media.push_audio_sample(out.reshape(-1, 1))
                except Exception as exc:  # noqa: BLE001
                    logger.debug("push_audio_sample failed: %s", exc)

            # 3) drive embodiment from status
            self._drive_motion(reachy_mini, time.time() - t0)

            time.sleep(0.02)  # ~50 Hz

        brain.join(timeout=5.0)
        for fn in ("stop_recording", "stop_playing"):
            try:
                getattr(media, fn)()
            except Exception:  # noqa: BLE001
                pass

    def _vad_feed(self, mono: np.ndarray) -> None:
        """Energy-VAD over a mic chunk; emit a finished utterance to the STT queue."""
        if self._speaking():
            # ignore mic while Reachy is talking; reset any partial capture
            self._in_speech, self._cap = False, []
            return
        if mono.shape[0] == 0:
            return
        rms = float(np.sqrt(np.mean(mono.astype(np.float32) ** 2)))
        now = time.time()
        if rms > VAD_THRESHOLD:
            if not self._in_speech:
                self._in_speech, self._cap = True, []
            self._cap.append(mono)
            self._last_voice = now
        elif self._in_speech:
            self._cap.append(mono)  # keep a little trailing silence
            if now - self._last_voice > VAD_SILENCE_S:
                audio = np.concatenate(self._cap) if self._cap else np.zeros(0, np.float32)
                self._in_speech, self._cap = False, []
                if audio.shape[0] >= int(VAD_MIN_S * self._in_sr):
                    self._stt_queue.put(audio)

    def _drive_motion(self, robot: ReachyMini, t: float) -> None:
        status = self._get_status()
        speaking = self._speaking()
        listening = self._in_speech

        # A queued recorded emotion takes over the body for its duration (motion only).
        with self._emo_lock:
            am = self._active_move
        if am is not None:
            move, start = am
            et = time.time() - start
            if et <= float(getattr(move, "duration", 0.0)):
                try:
                    head, ant, byaw = move.evaluate(et)
                    kw = {}
                    if head is not None:
                        kw["head"] = head
                    if ant is not None:
                        kw["antennas"] = ant
                    if byaw is not None:
                        kw["body_yaw"] = byaw
                    robot.set_target(**kw)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("emotion evaluate failed: %s", exc)
                return
            with self._emo_lock:
                self._active_move = None

        # Target pose in degrees. Keep oscillations slow + small; smoothing eases jumps.
        if speaking:
            tgt_yaw = 1.5 * math.sin(2 * math.pi * 0.45 * t)
            tgt_pitch = -2.0 + 2.0 * math.sin(2 * math.pi * 1.2 * t)   # gentle head-bob
            tgt_roll = 0.0
            tgt_al = tgt_ar = 28.0
        elif listening:
            tgt_yaw, tgt_pitch, tgt_roll = 0.0, -6.0, 0.0             # attentive, head up
            tgt_al = tgt_ar = 36.0
        elif status in (WorkerStatus.THINKING, WorkerStatus.WORKING):
            tgt_yaw = 2.0 * math.sin(2 * math.pi * 0.12 * t)
            tgt_pitch, tgt_roll = 4.0, -3.0                          # gentle pondering (not dropped low)
            tgt_al = tgt_ar = 16.0
        elif status == WorkerStatus.ASKING:
            tgt_yaw, tgt_pitch, tgt_roll = 0.0, -4.0, 10.0           # quizzical tilt
            tgt_al, tgt_ar = 40.0, 0.0
        else:  # IDLE — barely-there breathing
            tgt_yaw = 2.5 * math.sin(2 * math.pi * 0.06 * t)
            tgt_pitch = 1.5 * math.sin(2 * math.pi * 0.09 * t + 1.0)
            tgt_roll = 0.0
            tgt_al = 3.0 * math.sin(2 * math.pi * 0.08 * t)
            tgt_ar = -tgt_al

        a = MOTION_SMOOTH
        m = self._mt
        m["yaw"] += (tgt_yaw - m["yaw"]) * a
        m["pitch"] += (tgt_pitch - m["pitch"]) * a
        m["roll"] += (tgt_roll - m["roll"]) * a
        m["al"] += (tgt_al - m["al"]) * a
        m["ar"] += (tgt_ar - m["ar"]) * a

        try:
            robot.set_target(
                head=create_head_pose(yaw=m["yaw"], pitch=m["pitch"], roll=m["roll"], degrees=True),
                antennas=np.deg2rad([m["al"], m["ar"]]),
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("set_target failed: %s", exc)

    # ─── BRAIN thread: STT -> worker -> TTS (no robot calls) ──────────────────

    def _brain_thread(self, stop_event: threading.Event) -> None:
        import asyncio

        try:
            asyncio.run(self._brain_main(stop_event))
        except Exception:  # noqa: BLE001
            logger.exception("Brain thread crashed")

    async def _brain_main(self, stop_event: threading.Event) -> None:
        import asyncio

        loop = asyncio.get_event_loop()
        worker = WorkerSession(WORKER_CWD, name="w1")
        await worker.start()
        if await loop.run_in_executor(None, self._emotions.load):
            self._play_emotion("welcoming1")
        logger.info("Supervisor ready — say something to Reachy.")

        while not stop_event.is_set():
            audio = await loop.run_in_executor(None, self._next_utterance, 0.5)
            if audio is None:
                continue

            text = await loop.run_in_executor(None, self._stt.transcribe, audio)
            if not text:
                continue  # noise/empty clip — stay idle, don't flip the head into "thinking"
            logger.info("User: %s", text)
            self._set_status(WorkerStatus.THINKING)

            reply_parts: list[str] = []
            used_tool = False
            played_think = False
            async for ev in worker.ask(text):
                if ev.kind == "tool":
                    used_tool = True
                    self._set_status(WorkerStatus.WORKING)
                    if not played_think:
                        self._play_emotion("thoughtful1")
                        played_think = True
                elif ev.kind == "thinking":
                    self._set_status(WorkerStatus.THINKING)
                elif ev.kind == "text" and ev.text.strip():
                    reply_parts.append(ev.text.strip())
                elif ev.kind == "error":
                    reply_parts.append("Sorry, I ran into an error.")

            reply = " ".join(reply_parts).strip()
            logger.info("Reachy: %s", reply)
            if reply:
                self._set_status(WorkerStatus.SPEAKING)
                audio_out = await loop.run_in_executor(None, self._synthesize, reply)
                if audio_out is not None and audio_out.shape[0]:
                    self._enqueue_playback(audio_out)
                while not stop_event.is_set() and self._play_remaining() > 0:
                    await asyncio.sleep(0.05)
            if used_tool:
                self._play_emotion("success1")
                await asyncio.sleep(0.05)
            self._set_status(WorkerStatus.IDLE)

        await worker.stop()

    def _next_utterance(self, timeout: float) -> Optional[np.ndarray]:
        try:
            return self._stt_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def _synthesize(self, text: str) -> Optional[np.ndarray]:
        """Piper TTS -> float32 mono resampled to the robot's output sample rate."""
        try:
            sr, audio_i16 = self._tts.synthesize(text)
        except Exception:  # noqa: BLE001
            logger.exception("TTS failed")
            return None
        if audio_i16.shape[0] == 0:
            return None
        f = audio_i16.astype(np.float32) / 32768.0
        if sr != self._out_sr:
            from scipy.signal import resample_poly

            g = math.gcd(int(sr), int(self._out_sr))
            f = resample_poly(f, self._out_sr // g, sr // g)
        return f.astype(np.float32)


def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    app = ReachyFleetSupervisorApp()
    try:
        app.wrapped_run()
    except KeyboardInterrupt:
        app.stop()


if __name__ == "__main__":
    _main()
