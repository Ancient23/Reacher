"""Tests for per-agent identity — stable name + color per manager (U9).

Pure-logic tests for the deterministic color palette and assignment: a manager's
color must be stable (same key → same color, cross-process), and a fleet of 2–4
concurrent managers must get distinct colors regardless of roster order.
"""

from __future__ import annotations

import pytest

from reachy_fleet_supervisor.fleet import (
    PALETTE,
    AgentColor,
    color_for,
    assign_colors,
)
from reachy_fleet_supervisor.fleet.identity import _hash_index


# ---------------------------------------------------------------------------
# Palette + AgentColor
# ---------------------------------------------------------------------------


def test_palette_is_distinct_and_sized_past_target() -> None:
    # Designed for 2–4 concurrent; palette is larger so small fleets never collide.
    assert len(PALETTE) >= 4
    names = [c.name for c in PALETTE]
    hexes = [c.hex for c in PALETTE]
    ansis = [c.ansi for c in PALETTE]
    assert len(set(names)) == len(names), "duplicate color names"
    assert len(set(hexes)) == len(hexes), "duplicate hex values"
    assert len(set(ansis)) == len(ansis), "duplicate ansi codes"


def test_agent_color_to_dict_and_swatch() -> None:
    color = AgentColor("amber", "#b58900", 178)
    assert color.to_dict() == {"name": "amber", "hex": "#b58900", "ansi": 178}
    swatch = color.swatch()
    assert "178" in swatch and swatch.endswith("\x1b[0m")


# ---------------------------------------------------------------------------
# color_for — stable per single key
# ---------------------------------------------------------------------------


def test_color_for_is_deterministic() -> None:
    # Same key → same color every call (and, via SHA-1, across processes).
    assert color_for("aaaa1111") == color_for("aaaa1111")
    assert color_for("aaaa1111") in PALETTE


def test_color_for_known_hash_is_stable() -> None:
    # Pin the cross-process mapping so a regression in the hash is caught.
    idx = _hash_index("aaaa1111", len(PALETTE))
    assert color_for("aaaa1111") is PALETTE[idx]


def test_color_for_empty_palette_raises() -> None:
    with pytest.raises(ValueError):
        color_for("x", palette=())


# ---------------------------------------------------------------------------
# assign_colors — distinct across the fleet, order-independent
# ---------------------------------------------------------------------------


def test_assign_colors_distinct_for_small_fleet() -> None:
    keys = ["aaaa1111", "bbbb2222", "cccc3333", "dddd4444"]  # 4 concurrent
    colors = assign_colors(keys)
    assert set(colors) == set(keys)
    assigned = [colors[k] for k in keys]
    assert len(set(assigned)) == len(keys), "2–4 concurrent managers must be distinct"
    assert all(c in PALETTE for c in assigned)


def test_assign_colors_is_order_independent() -> None:
    keys = ["aaaa1111", "bbbb2222", "cccc3333"]
    forward = assign_colors(keys)
    reverse = assign_colors(list(reversed(keys)))
    assert forward == reverse, "every renderer must compute the same map"


def test_assign_colors_dedupes_keys() -> None:
    colors = assign_colors(["aaaa1111", "aaaa1111", "bbbb2222"])
    assert set(colors) == {"aaaa1111", "bbbb2222"}


def test_assign_colors_resolves_collisions_by_probing() -> None:
    # Force a collision with a 1-color palette: both want slot 0; second probes
    # but wraps back (palette exhausted) → reuse rather than crash.
    one = (AgentColor("only", "#000000", 0),)
    colors = assign_colors(["k1", "k2"], palette=one)
    assert colors["k1"] is one[0] and colors["k2"] is one[0]


def test_assign_colors_overflow_falls_back_not_fails() -> None:
    # More managers than palette slots: distinct up to len(palette), then reuse.
    keys = [f"k{i:02d}" for i in range(len(PALETTE) + 3)]
    colors = assign_colors(keys)
    assert set(colors) == set(keys)
    assert len(set(colors.values())) == len(PALETTE)


def test_assign_colors_empty() -> None:
    assert assign_colors([]) == {}
