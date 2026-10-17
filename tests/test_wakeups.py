"""Tests that the vehicle is not woken more often than intended.

Plain polls only read what the cloud has cached. A deep refresh is the one
automatic action that wakes the vehicle, so its limits are pinned down here.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from homeassistant.core import State
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    mock_restore_cache,
)

from custom_components.uconnect.const import CONF_ADD_COMMAND_ENTITIES
from custom_components.uconnect.extrapolated_soc import (
    MAX_DEEP_REFRESHES_PER_SESSION,
    SocEstimationState,
)

from .conftest import VIN


def deep_refreshes(mock_client) -> int:
    return sum(
        1
        for call in mock_client.command_verify.call_args_list
        if call.args[1].name == "DEEPREFRESH"
    )


async def step(hass, freezer, minutes: float) -> None:
    """Move the frozen clock forward and run what became due."""
    freezer.tick(timedelta(minutes=minutes))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_plain_polling_never_wakes_the_vehicle(hass, freezer, config_entry, mock_client, vehicle):
    vehicle.charging = False
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    # A day of polls while parked
    for _ in range(288):
        await step(hass, freezer, 5.01)

    mock_client.command_verify.assert_not_called()
    assert mock_client.refresh.call_count > 200


async def test_charging_day_stays_within_cap(hass, freezer, config_entry, mock_client):
    config_entry.add_to_hass(hass)
    # The shortest interval the options allow
    hass.config_entries.async_update_entry(
        config_entry,
        options={**config_entry.options, "charging_refresh_interval": 15},
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    # 24 hours in one-minute steps with the cloud reporting "charging" all along
    for _ in range(24 * 60):
        await step(hass, freezer, 1)

    assert deep_refreshes(mock_client) == MAX_DEEP_REFRESHES_PER_SESSION


def restore_recent_wake_up(hass) -> None:
    """Pretend the previous run woke the charging vehicle two minutes ago."""
    woken = dt_util.utcnow() - timedelta(minutes=2)
    state = SocEstimationState(
        last_actual_soc=72,
        last_actual_soc_time=woken,
        last_vehicle_soc=72,
        is_charging=True,
        last_deep_refresh=woken,
        deep_refreshes_this_session=1,
    )
    mock_restore_cache(
        hass,
        [
            State(
                "sensor.fiat_500e_extrapolated_battery",
                "72",
                {"estimation_state": state.to_dict()},
            )
        ],
    )


async def test_restart_while_charging_does_not_wake_again(
    hass, freezer, config_entry, mock_client
):
    # Recurring refresh off: the session already had its wake-up
    restore_recent_wake_up(hass)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    soc = next(
        e for e in hass.states.async_all("sensor") if "extrapolated" in e.entity_id
    )
    assert soc.attributes["deep_refreshes_this_session"] == 1, soc.entity_id

    for _ in range(12):
        await step(hass, freezer, 10)
    assert deep_refreshes(mock_client) == 0


async def test_restart_with_recurring_refresh_waits_for_spacing(
    hass, freezer, config_entry, mock_client
):
    restore_recent_wake_up(hass)
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        options={**config_entry.options, "charging_refresh_interval": 30},
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    # The usual restart delay passes without a wake-up, it is held back
    # until 15 minutes after the last one
    await step(hass, freezer, 5)
    assert deep_refreshes(mock_client) == 0
    await step(hass, freezer, 9)
    assert deep_refreshes(mock_client) == 1


async def test_repeated_restarts_while_charging(hass, freezer, config_entry, mock_client):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    await step(hass, freezer, 4)
    assert deep_refreshes(mock_client) == 1

    # Reload five times in quick succession, as a restart would
    for _ in range(5):
        assert await hass.config_entries.async_reload(config_entry.entry_id)
        await hass.async_block_till_done()
        await step(hass, freezer, 4)

    assert deep_refreshes(mock_client) == 1


async def test_without_pin_nothing_wakes(hass, freezer, config_entry, mock_client):
    mock_client.api.pin = ""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    for _ in range(12):
        await step(hass, freezer, 10)

    mock_client.command_verify.assert_not_called()


def test_state_round_trip_keeps_wake_up_history():
    woken = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    state = SocEstimationState(last_deep_refresh=woken, deep_refreshes_this_session=4)
    restored = SocEstimationState.from_dict(state.to_dict())
    assert restored.last_deep_refresh == woken
    assert restored.deep_refreshes_this_session == 4

    # States saved by older versions have neither field
    old = state.to_dict()
    del old["last_deep_refresh"], old["deep_refreshes_this_session"]
    restored = SocEstimationState.from_dict(old)
    assert restored.last_deep_refresh is None
    assert restored.deep_refreshes_this_session == 0


async def test_restarts_with_recurring_refresh_keep_spacing(
    hass, freezer, config_entry, mock_client
):
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        options={**config_entry.options, "charging_refresh_interval": 30},
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    # An hour with a restart every five minutes
    for _ in range(12):
        await step(hass, freezer, 5)
        assert await hass.config_entries.async_reload(config_entry.entry_id)
        await hass.async_block_till_done()

    # Restarts reset the 30 minute timer, but never below the 15 minute
    # spacing: at most one wake-up per quarter hour
    assert 1 <= deep_refreshes(mock_client) <= 4
