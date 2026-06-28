"""Pollen's recorded emotion library — expressive reactions for the supervisor.

Loads `pollen-robotics/reachy-mini-emotions-library` (81 named moves) via core
`reachy_mini`. We use motion only (head/antennas/body trajectory); the app plays
these through its single set_target loop, so they never fight the idle motion.
Audio sidecars are ignored here to avoid clashing with Piper TTS.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

EMOTIONS_REPO = "pollen-robotics/reachy-mini-emotions-library"


class EmotionLibrary:
    """Lazy wrapper over RecordedMoves. Thread-safe; degrades gracefully if offline."""

    def __init__(self, repo: str = EMOTIONS_REPO) -> None:
        self._repo = repo
        self._rm: Any = None
        self._names: List[str] = []
        self._lock = threading.Lock()

    def load(self) -> bool:
        """Download/parse the library (first call may fetch from HF). Returns availability."""
        with self._lock:
            if self._rm is not None:
                return True
            try:
                from reachy_mini.motion.recorded_move import RecordedMoves

                self._rm = RecordedMoves(self._repo)
                self._names = list(self._rm.list_moves())
                logger.info("Loaded %d emotions from %s", len(self._names), self._repo)
                return True
            except Exception:  # noqa: BLE001
                logger.exception("Emotion library unavailable")
                return False

    @property
    def names(self) -> List[str]:
        return self._names

    def get(self, name: str) -> Optional[Any]:
        """Return a Move object (has .duration and .evaluate(t)) or None."""
        if self._rm is None and not self.load():
            return None
        if name not in self._names:
            logger.warning("Unknown emotion '%s' (have %d)", name, len(self._names))
            return None
        try:
            return self._rm.get(name)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to load emotion '%s'", name)
            return None
