"""Per-agent identity — stable name + color per manager (U9).

A manager already has a stable **name** (its voice handle, e.g. "the unreal
one"; minted on spawn and kept unique). U9 adds the other half of identity a
human needs to tell 2–4 concurrent managers apart at a glance: a stable
**color**. The same color is shown by every renderer — the headless CLI, the web
dashboard, and (later) the robot body / antennas — so "the amber one" means the
same manager everywhere (plan.md §3 "one source of truth, many renderers"; §4
Phase 2 per-agent identity).

Two design constraints pull against each other:

- *Stable per manager* — a manager should keep its color across polls and app
  restarts, and every renderer (separate processes) must agree on it. So the
  assignment is **deterministic from the manager's stable key** (its short id),
  using :func:`hashlib.sha1` rather than the salted built-in ``hash`` so it is
  identical across processes.
- *Distinct for the fleet* — with 2–4 managers up at once you want no two sharing
  a color. So :func:`assign_colors` takes the whole set of keys and resolves
  hash collisions by deterministic probing: distinct colors are guaranteed while
  the fleet is no larger than the palette, and the result is independent of
  roster order (keys are sorted first) so all renderers compute the same map.

The palette is sized past the 2–4 concurrent target (decision: 6 distinct,
high-contrast hues) with a hex (web/body) and an ANSI-256 code (terminal swatch)
for each, so every renderer can paint the same identity in its own medium.
"""

from __future__ import annotations
import hashlib
from typing import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class AgentColor:
    """One palette entry: a named color with a web hex and a terminal ANSI code.

    ``name`` is the human/voice label ("amber"); ``hex`` drives the dashboard and
    the robot body; ``ansi`` is the 256-color code for a terminal swatch.
    """

    name: str
    hex: str
    ansi: int

    def to_dict(self) -> dict[str, object]:
        """JSON-ready dict (what the CLI/dashboard serialize)."""
        return {"name": self.name, "hex": self.hex, "ansi": self.ansi}

    def swatch(self) -> str:
        """Return a small colored block for a 256-color terminal (resets after)."""
        return f"\x1b[38;5;{self.ansi}m●\x1b[0m"


# High-contrast, visually distinct hues — sized past the 2–4 concurrent target so
# small fleets never collide. Order is the deterministic preference order.
PALETTE: tuple[AgentColor, ...] = (
    AgentColor("cyan", "#2aa198", 37),
    AgentColor("amber", "#b58900", 178),
    AgentColor("blue", "#268bd2", 33),
    AgentColor("magenta", "#d33682", 170),
    AgentColor("green", "#859900", 70),
    AgentColor("orange", "#cb4b16", 208),
)


def _hash_index(key: str, modulus: int) -> int:
    """Deterministic, cross-process index for *key* in ``[0, modulus)``.

    Uses SHA-1 (stable across runs/processes) rather than the built-in ``hash``
    (which is per-process salted) so every renderer maps a manager to the same
    color.
    """
    digest = hashlib.sha1(key.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], "big") % modulus


def color_for(key: str, *, palette: Sequence[AgentColor] = PALETTE) -> AgentColor:
    """Stable color for a single *key*, ignoring the rest of the fleet.

    Deterministic from *key* alone — handy for rendering one manager in
    isolation. For a coordinated, collision-free set use :func:`assign_colors`.
    """
    if not palette:
        raise ValueError("palette must not be empty")
    return palette[_hash_index(key, len(palette))]


def assign_colors(
    keys: Sequence[str], *, palette: Sequence[AgentColor] = PALETTE
) -> dict[str, AgentColor]:
    """Map each unique key in *keys* to a color, distinct while the fleet fits.

    Each key prefers its :func:`color_for` slot; on collision it probes to the
    next free palette slot, so no two managers share a color while the fleet is
    no larger than the palette. Keys are processed in sorted order, so the result
    is independent of roster order — every renderer (CLI, dashboard, body)
    computes the same map. If the fleet outgrows the palette, the overflow keys
    fall back to their preferred (now reused) slot rather than failing.
    """
    if not palette:
        raise ValueError("palette must not be empty")
    n = len(palette)
    result: dict[str, AgentColor] = {}
    used: set[int] = set()
    # dict.fromkeys de-dupes while we sort for deterministic, order-independent
    # assignment across renderers.
    for key in sorted(dict.fromkeys(keys)):
        start = _hash_index(key, n)
        idx = start
        for _ in range(n):
            if idx not in used:
                break
            idx = (idx + 1) % n
        else:  # palette exhausted — reuse the preferred slot
            idx = start
        used.add(idx)
        result[key] = palette[idx]
    return result
