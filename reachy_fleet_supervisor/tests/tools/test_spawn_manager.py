"""Tests for the spawn_manager tool's confirm-before-kickoff flow (U17)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from reachy_fleet_supervisor.profiles._reachy_fleet_supervisor_locked_profile.spawn_manager import (
    SpawnManager,
)
from reachy_fleet_supervisor.tools.core_tools import ToolDependencies


def _deps() -> ToolDependencies:
    return ToolDependencies(reachy_mini=MagicMock(), movement_manager=MagicMock())


@pytest.mark.asyncio
async def test_first_call_without_confirmed_does_not_spawn(tmp_path):
    tool = SpawnManager()
    with patch(
        "reachy_fleet_supervisor.fleet.runtime.get_fleet_runtime"
    ) as get_runtime:
        result = await tool(
            _deps(),
            name="tester",
            task="add unit tests",
            path=str(tmp_path),
        )

    get_runtime.assert_not_called()
    assert result["status"] == "needs_confirmation"
    assert "confirmation" in result
    assert "tester" in result["confirmation"]
    assert "go ahead" in result["confirmation"].lower()


@pytest.mark.asyncio
async def test_confirmed_true_spawns(tmp_path):
    tool = SpawnManager()
    fake_manager = MagicMock(name="fake_manager")
    fake_manager.name = "tester"
    fake_manager.id = "abc123"

    fake_runtime = MagicMock()
    fake_runtime.session_manager.spawn.return_value = fake_manager
    fake_runtime.poller.poll_once.return_value = None

    with patch(
        "reachy_fleet_supervisor.fleet.runtime.get_fleet_runtime",
        return_value=fake_runtime,
    ):
        result = await tool(
            _deps(),
            name="tester",
            task="add unit tests",
            path=str(tmp_path),
            confirmed=True,
        )

    fake_runtime.session_manager.spawn.assert_called_once()
    assert result["status"] == "spawned"
    assert result["name"] == "tester"


@pytest.mark.asyncio
async def test_missing_confirmed_is_treated_as_false(tmp_path):
    """Omitting 'confirmed' entirely (the normal first-call shape) must not spawn."""
    tool = SpawnManager()
    with patch(
        "reachy_fleet_supervisor.fleet.runtime.get_fleet_runtime"
    ) as get_runtime:
        result = await tool(
            _deps(),
            name="tester",
            task="add unit tests",
            path=str(tmp_path),
            confirmed=False,
        )

    get_runtime.assert_not_called()
    assert result["status"] == "needs_confirmation"
