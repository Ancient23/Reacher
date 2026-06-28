"""Local voice layer for the Reachy fleet supervisor (Option A — no cloud, no API key).

- Speech-to-text via faster-whisper (CTranslate2). Model auto-downloads on first use.
- Text-to-speech via Piper (onnxruntime). Voice model auto-downloads from HF on first use.

Both run fully locally — instant on the RTX 5090, fine on CPU for short utterances.
Audio is exchanged as float32 mono in [-1, 1]; the app resamples to/from the robot's
16 kHz media pipeline.

Self-test:
    python -m reachy_fleet_supervisor.voice "Hello, I am Reachy."
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# Default models (overridable via env). Small/medium balance speed vs quality;
# bump these on the 5090.
WHISPER_MODEL = os.getenv("FLEET_WHISPER_MODEL", "base.en")
WHISPER_DEVICE = os.getenv("FLEET_WHISPER_DEVICE", "cpu")          # "cuda" on the 5090
WHISPER_COMPUTE = os.getenv("FLEET_WHISPER_COMPUTE", "int8")        # "float16" on cuda

PIPER_VOICE = os.getenv("FLEET_PIPER_VOICE", "en_US-amy-medium")
PIPER_REPO = "rhasspy/piper-voices"
# rhasspy/piper-voices layout: <family>/<lang>/<speaker>/<quality>/<voice>.onnx
_pv = PIPER_VOICE.split("-")
_PIPER_LANG = _pv[0]                                     # e.g. "en_US"
_PIPER_SPEAKER = _pv[1] if len(_pv) > 1 else "default"  # e.g. "lessac"
_PIPER_QUALITY = _pv[2] if len(_pv) > 2 else "medium"   # e.g. "medium"
_PIPER_LANG_FAMILY = _PIPER_LANG.split("_")[0]          # e.g. "en"

MODELS_DIR = Path(os.getenv("FLEET_MODELS_DIR", str(Path.home() / ".cache" / "reachy_fleet" / "piper")))


# ─── Speech to text ──────────────────────────────────────────────────────────


class WhisperSTT:
    """Lazy faster-whisper wrapper. transcribe() takes float32 mono 16 kHz audio."""

    def __init__(self, model: str = WHISPER_MODEL, device: str = WHISPER_DEVICE,
                 compute_type: str = WHISPER_COMPUTE) -> None:
        self._model_name = model
        self._device = device
        self._compute_type = compute_type
        self._model = None

    def _ensure(self) -> None:
        if self._model is None:
            from faster_whisper import WhisperModel
            logger.info("Loading Whisper model '%s' (%s/%s)...", self._model_name,
                        self._device, self._compute_type)
            self._model = WhisperModel(self._model_name, device=self._device,
                                       compute_type=self._compute_type)

    def transcribe(self, audio_16k_mono: np.ndarray) -> str:
        """Return the recognized text for a float32 mono 16 kHz clip ('' if none)."""
        self._ensure()
        audio = np.ascontiguousarray(audio_16k_mono.astype(np.float32))
        segments, _info = self._model.transcribe(
            audio, language="en", vad_filter=True, beam_size=1,
        )
        return " ".join(seg.text for seg in segments).strip()


# ─── Text to speech ──────────────────────────────────────────────────────────


def _download_piper_voice() -> Path:
    """Ensure the Piper .onnx (+ .onnx.json) are present locally; return the .onnx path."""
    from huggingface_hub import hf_hub_download

    onnx_name = f"{PIPER_VOICE}.onnx"
    local_onnx = MODELS_DIR / onnx_name
    if local_onnx.exists() and (MODELS_DIR / f"{onnx_name}.json").exists():
        return local_onnx

    MODELS_DIR.mkdir(parents=True, exist_ok=True)
    # rhasspy/piper-voices layout: <lang_family>/<lang>/<speaker>/<quality>/<file>
    subdir = f"{_PIPER_LANG_FAMILY}/{_PIPER_LANG}/{_PIPER_SPEAKER}/{_PIPER_QUALITY}"
    for fname in (onnx_name, f"{onnx_name}.json"):
        logger.info("Downloading Piper asset %s ...", fname)
        path = hf_hub_download(repo_id=PIPER_REPO, filename=f"{subdir}/{fname}")
        target = MODELS_DIR / fname
        if not target.exists():
            import shutil
            shutil.copy(path, target)
    return local_onnx


class PiperTTS:
    """Lazy Piper wrapper. synthesize() returns (sample_rate, int16 mono audio)."""

    def __init__(self, voice: str = PIPER_VOICE) -> None:
        self._voice_id = voice
        self._voice = None

    def _ensure(self) -> None:
        if self._voice is None:
            from piper import PiperVoice
            onnx_path = _download_piper_voice()
            logger.info("Loading Piper voice '%s'...", self._voice_id)
            self._voice = PiperVoice.load(str(onnx_path))

    def synthesize(self, text: str) -> Tuple[int, np.ndarray]:
        """Synthesize text -> (sample_rate, int16 mono numpy array)."""
        self._ensure()
        text = text.strip()
        if not text:
            return self._sample_rate(), np.zeros(0, dtype=np.int16)

        # Piper's API has shifted across releases; handle both shapes.
        try:
            chunks = list(self._voice.synthesize(text))
            if chunks and hasattr(chunks[0], "audio_int16_bytes"):
                raw = b"".join(c.audio_int16_bytes for c in chunks)
                sr = getattr(chunks[0], "sample_rate", self._sample_rate())
                return sr, np.frombuffer(raw, dtype=np.int16)
        except (AttributeError, TypeError):
            pass

        # Older API: streaming raw PCM.
        raw = b"".join(self._voice.synthesize_stream_raw(text))
        return self._sample_rate(), np.frombuffer(raw, dtype=np.int16)

    def _sample_rate(self) -> int:
        cfg = getattr(self._voice, "config", None)
        return int(getattr(cfg, "sample_rate", 22050)) if cfg is not None else 22050


# ─── Self-test ───────────────────────────────────────────────────────────────


def _selftest(text: str) -> None:
    logging.basicConfig(level=logging.INFO)
    tts = PiperTTS()
    sr, audio = tts.synthesize(text)
    print(f"Piper: {audio.shape[0]} samples @ {sr} Hz")

    # Round-trip through Whisper by resampling 22k->16k float.
    from scipy.signal import resample_poly
    f = audio.astype(np.float32) / 32768.0
    f16 = resample_poly(f, 16000, sr) if sr != 16000 else f
    stt = WhisperSTT()
    print("Whisper heard:", repr(stt.transcribe(f16)))


if __name__ == "__main__":
    _selftest(sys.argv[1] if len(sys.argv) > 1 else "Hello, I am Reachy Mini, your fleet supervisor.")
