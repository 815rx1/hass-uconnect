"""Tests that the vehicle is not woken more often than intended.

Plain polls only read what the cloud has cached. The deep refresh while
charging is the one automatic action that wakes the vehicle, so its limits are
pinned down here, through the whole integration with the clock moving.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import State
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    async_fire_time_changed,
    mock_restore_cache,
)

from custom_components.uconnect.const import DOMAIN
from custom_components.uconnect.extrapolated_soc import SocEstimationState
from custom_components.uconnect.wake_guard import (
    AUTO_REFRESH_MAX_PER_DAY,
    AUTO_REFRESH_MAX_WITHOUT_PROGRESS,
    MANUAL_REFRESH_COOLDOWN,
    STORAGE_KEY,
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


async def run_for(hass, freezer, minutes: int, every: float = 1) -> None:
    elapsed = 0.0
    while elapsed < minutes:
        await step(hass, freezer, every)
        elapsed += every


def charging_car(mock_client, vehicle, gain: float = 1) -> None:
    """Make the vehicle a charging one that uploads on every deep refresh."""
    vehicle.charging = True
    vehicle.plugged_in = True
    vehicle.state_of_charge = 10
    vehicle.timestamp_ev_info = dt_util.utcnow()

    def command(vin, cmd):
        if cmd.name == "DEEPREFRESH":
            vehicle.state_of_charge = min(vehicle.state_of_charge + gain, 99)
            vehicle.timestamp_ev_info = dt_util.utcnow()
        return True

    mock_client.command_verify.side_effect = command


async def setup(hass, config_entry, interval: int) -> None:
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        options={**config_entry.options, "charging_refresh_interval": interval},
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


async def test_plain_polling_never_wakes_the_vehicle(
    hass, freezer, config_entry, mock_client, vehicle
):
    vehicle.charging = False
    await setup(hass, config_entry, 30)

    # A day of polls while parked
    await run_for(hass, freezer, 24 * 60, every=5.01)

    mock_client.command_verify.assert_not_called()
    assert mock_client.refresh.call_count > 200


async def test_interval_zero_refreshes_once_per_charge(
    hass, freezer, config_entry, mock_client, vehicle
):
    charging_car(mock_client, vehicle)
    await setup(hass, config_entry, 0)

    await run_for(hass, freezer, 12 * 60, every=3)
    assert deep_refreshes(mock_client) == 1


async def test_refresh_when_a_charge_begins(
    hass, freezer, config_entry, mock_client, vehicle
):
    vehicle.charging = False
    await setup(hass, config_entry, 0)
    # Past the startup grace, parked
    await run_for(hass, freezer, 30, every=2)
    assert deep_refreshes(mock_client) == 0

    # Plugged in: the next poll sees the charge, three minutes later the
    # vehicle is asked for fresh data
    charging_car(mock_client, vehicle)
    await config_entry.runtime_data.async_refresh()
    await run_for(hass, freezer, 2)
    assert deep_refreshes(mock_client) == 0
    await run_for(hass, freezer, 2)
    assert deep_refreshes(mock_client) == 1

    # Charge over: nothing more
    vehicle.charging = False
    await config_entry.runtime_data.async_refresh()
    await run_for(hass, freezer, 6 * 60, every=3)
    assert deep_refreshes(mock_client) == 1


async def test_charging_day_stays_within_daily_limit(
    hass, freezer, config_entry, mock_client, vehicle
):
    charging_car(mock_client, vehicle, gain=0.5)
    # The shortest interval the options allow
    await setup(hass, config_entry, 15)

    await run_for(hass, freezer, 24 * 60)
    assert deep_refreshes(mock_client) == AUTO_REFRESH_MAX_PER_DAY


async def test_slow_charge_is_followed_for_twelve_hours(
    hass, freezer, config_entry, mock_client, vehicle
):
    charging_car(mock_client, vehicle)
    await setup(hass, config_entry, 30)

    # The first one only after the startup grace, then every 30 minutes
    await run_for(hass, freezer, 14)
    assert deep_refreshes(mock_client) == 0
    await run_for(hass, freezer, 12 * 60 - 14)
    assert deep_refreshes(mock_client) == 24


async def test_finished_charge_stops_waking(
    hass, freezer, config_entry, mock_client, vehicle
):
    charging_car(mock_client, vehicle, gain=0)
    await setup(hass, config_entry, 15)

    await run_for(hass, freezer, 24 * 60)

    # One to take the baseline, then the allowed refreshes without new data
    assert deep_refreshes(mock_client) == 1 + AUTO_REFRESH_MAX_WITHOUT_PROGRESS


@pytest.mark.parametrize(
    "change",
    [
        {"plugged_in": False},
        {"state_of_charge": None},
        {"state_of_charge": 100},
        {"charging": None},
        {"charging": False},
    ],
)
async def test_no_wake_without_a_charge_to_follow(
    hass, freezer, config_entry, mock_client, vehicle, change
):
    charging_car(mock_client, vehicle)
    for key, value in change.items():
        setattr(vehicle, key, value)
    await setup(hass, config_entry, 15)

    await run_for(hass, freezer, 3 * 60, every=2)
    assert deep_refreshes(mock_client) == 0


async def test_no_wake_on_old_charge_data(
    hass, freezer, config_entry, mock_client, vehicle
):
    charging_car(mock_client, vehicle)
    # The cloud says charging, but the vehicle sent that a day ago
    vehicle.timestamp_ev_info = dt_util.utcnow() - timedelta(days=1)
    mock_client.command_verify.side_effect = None
    await setup(hass, config_entry, 15)

    await run_for(hass, freezer, 3 * 60, every=2)
    assert deep_refreshes(mock_client) == 0


async def test_no_wake_while_updates_fail(
    hass, freezer, config_entry, mock_client, vehicle
):
    charging_car(mock_client, vehicle)
    await setup(hass, config_entry, 15)
    mock_client.refresh.side_effect = Exception("cloud down")

    await run_for(hass, freezer, 3 * 60, every=2)
    assert deep_refreshes(mock_client) == 0


async def test_flapping_charge_does_not_multiply_wakes(
    hass, freezer, config_entry, mock_client, vehicle
):
    charging_car(mock_client, vehicle)
    await setup(hass, config_entry, 30)
    coordinator = config_entry.runtime_data

    # Solar surplus charging: the wallbox starts and stops every 5 minutes
    for i in range(8 * 12):
        vehicle.charging = i % 2 == 0
        vehicle.timestamp_ev_info = dt_util.utcnow()
        await coordinator.async_refresh()
        await run_for(hass, freezer, 5)

    # At most one per interval, as if it had charged without a break
    assert 1 <= deep_refreshes(mock_client) <= 16


async def test_flapping_charge_with_interval_zero(
    hass, freezer, config_entry, mock_client, vehicle
):
    charging_car(mock_client, vehicle)
    await setup(hass, config_entry, 0)
    coordinator = config_entry.runtime_data

    for i in range(4 * 12):
        vehicle.charging = i % 2 == 0
        vehicle.timestamp_ev_info = dt_util.utcnow()
        await coordinator.async_refresh()
        await run_for(hass, freezer, 5)

    # One charge with short breaks, one refresh
    assert deep_refreshes(mock_client) == 1


async def test_restart_keeps_the_regular_schedule(
    hass, freezer, hass_storage, config_entry, mock_client, vehicle
):
    # The previous run woke the charging vehicle two minutes ago and wrote
    # that down before sending
    now = dt_util.utcnow()
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "minor_version": 1,
        "key": STORAGE_KEY,
        "data": {
            "vehicles": {
                VIN: {
                    "wakes": [
                        {
                            "at": (now - timedelta(minutes=2)).isoformat(),
                            "command": "DEEPREFRESH",
                            "automatic": True,
                        }
                    ],
                    "session": {
                        "started": (now - timedelta(minutes=30)).isoformat(),
                        "auto_refreshes": 1,
                    },
                }
            }
        },
    }
    charging_car(mock_client, vehicle)
    await setup(hass, config_entry, 30)

    # Due 30 minutes after the last one, that is in 28
    await run_for(hass, freezer, 27)
    assert deep_refreshes(mock_client) == 0
    await run_for(hass, freezer, 2)
    assert deep_refreshes(mock_client) == 1


async def test_wake_is_on_disk_before_the_command(
    hass, freezer, hass_storage, config_entry, mock_client, vehicle
):
    charging_car(mock_client, vehicle)
    seen = []

    def command(vin, cmd):
        seen.append(
            [w["command"] for w in hass_storage[STORAGE_KEY]["data"]["vehicles"][VIN]["wakes"]]
        )
        return True

    mock_client.command_verify.side_effect = command
    await setup(hass, config_entry, 30)
    await run_for(hass, freezer, 20)

    assert seen == [["DEEPREFRESH"]]


async def test_legacy_wake_history_is_taken_over(
    hass, freezer, hass_storage, config_entry, mock_client, vehicle
):
    charging_car(mock_client, vehicle)
    recently = dt_util.utcnow() - timedelta(minutes=5)
    state = SocEstimationState(
        last_actual_soc=10, last_actual_soc_time=recently, is_charging=True
    ).to_dict()
    # What 0.6.0b3 to b6 kept in the restored attributes
    state["deep_refresh_times"] = [recently.isoformat()]
    mock_restore_cache(
        hass,
        [
            State(
                "sensor.fiat_500e_extrapolated_battery",
                "10",
                {"estimation_state": state},
            )
        ],
    )
    await setup(hass, config_entry, 30)

    # The startup grace and the interval from the wake taken over both hold
    await run_for(hass, freezer, 24)
    assert deep_refreshes(mock_client) == 0
    await run_for(hass, freezer, 3)
    assert deep_refreshes(mock_client) == 1


async def test_interval_set_to_zero_mid_charge(
    hass, freezer, config_entry, mock_client, vehicle
):
    charging_car(mock_client, vehicle)
    await setup(hass, config_entry, 30)
    await run_for(hass, freezer, 20)
    assert deep_refreshes(mock_client) == 1

    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, "charging_refresh_interval": 0}
    )
    await hass.async_block_till_done()
    await run_for(hass, freezer, 6 * 60, every=3)

    # The one at the start was all
    assert deep_refreshes(mock_client) == 1


async def test_shorter_interval_does_not_bring_the_next_one_forward(
    hass, freezer, config_entry, mock_client, vehicle
):
    charging_car(mock_client, vehicle)
    await setup(hass, config_entry, 60)
    await run_for(hass, freezer, 20)
    assert deep_refreshes(mock_client) == 1

    # Changed to 15 minutes five minutes after the refresh: the next one
    # still waits for the 15 minute spacing, not less
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, "charging_refresh_interval": 15}
    )
    await hass.async_block_till_done()
    await run_for(hass, freezer, 9)
    assert deep_refreshes(mock_client) == 1
    await run_for(hass, freezer, 3)
    assert deep_refreshes(mock_client) == 2


async def test_failures_end_the_session_also_across_reloads(
    hass, freezer, config_entry, mock_client, vehicle
):
    charging_car(mock_client, vehicle)
    mock_client.command_verify.side_effect = Exception("vehicle not answering")
    await setup(hass, config_entry, 15)

    await run_for(hass, freezer, 3 * 60, every=2)
    assert mock_client.command_verify.call_count == 3

    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    await run_for(hass, freezer, 3 * 60, every=2)
    assert mock_client.command_verify.call_count == 3


async def test_manual_refresh_is_held_back_briefly(
    hass, freezer, config_entry, mock_client, vehicle
):
    vehicle.charging = False
    await setup(hass, config_entry, 30)
    button = er.async_get(hass).async_get_entity_id(
        "button", DOMAIN, f"{DOMAIN}_{VIN}_button_deep_refresh"
    )

    await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "button", "press", {"entity_id": button}, blocking=True
        )
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "refresh_location", {}, blocking=True)
    assert mock_client.command_verify.call_count == 1

    await run_for(hass, freezer, MANUAL_REFRESH_COOLDOWN.total_seconds() / 60 + 1)
    await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
    assert mock_client.command_verify.call_count == 2


async def test_automation_loop_is_cut_off(
    hass, freezer, config_entry, mock_client, vehicle
):
    vehicle.charging = False
    await setup(hass, config_entry, 30)

    # An automation asking for a deep refresh every time something changed,
    # which the refresh itself makes happen
    for _ in range(60):
        try:
            await hass.services.async_call(DOMAIN, "deep_refresh", {}, blocking=True)
        except ServiceValidationError:
            pass
        await step(hass, freezer, 1)

    assert deep_refreshes(mock_client) <= 6


async def test_command_holds_the_automatic_refresh(
    hass, freezer, config_entry, mock_client, vehicle
):
    charging_car(mock_client, vehicle)
    await setup(hass, config_entry, 30)
    await run_for(hass, freezer, 14)

    # Locked by hand a minute before the refresh would be due
    await hass.services.async_call(DOMAIN, "doors_lock", {}, blocking=True)
    await run_for(hass, freezer, 14)
    assert deep_refreshes(mock_client) == 0
    await run_for(hass, freezer, 2)
    assert deep_refreshes(mock_client) == 1


async def test_pin_change_lets_failed_refreshes_retry(
    hass, freezer, config_entry, mock_client, vehicle
):
    charging_car(mock_client, vehicle)
    mock_client.command_verify.side_effect = Exception("wrong PIN")
    await setup(hass, config_entry, 15)
    await run_for(hass, freezer, 2 * 60, every=2)
    assert mock_client.command_verify.call_count == 3

    charging_car(mock_client, vehicle)
    mock_client.api.pin = "1234"
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, "pin": "5678"}
    )
    await hass.async_block_till_done()
    # Tried again shortly after, and once more after the 15 minute interval
    await run_for(hass, freezer, 30, every=2)
    assert deep_refreshes(mock_client) == 5


async def test_remembered_charge_alone_does_not_wake(
    hass, freezer, config_entry, mock_client, vehicle
):
    # Charging when Home Assistant stopped, and the status after the start
    # leaves the charging flag out: what was remembered is no proof
    charging_car(mock_client, vehicle)
    vehicle.charging = None
    state = SocEstimationState(
        last_actual_soc=10,
        last_actual_soc_time=dt_util.utcnow() - timedelta(minutes=10),
        last_vehicle_soc=10,
        is_charging=True,
    )
    mock_restore_cache(
        hass,
        [
            State(
                "sensor.fiat_500e_extrapolated_battery",
                "10",
                {"estimation_state": state.to_dict()},
            )
        ],
    )
    await setup(hass, config_entry, 15)

    await run_for(hass, freezer, 3 * 60, every=2)
    assert deep_refreshes(mock_client) == 0

    # Once the vehicle reports charging again it is followed
    vehicle.charging = True
    vehicle.timestamp_ev_info = dt_util.utcnow()
    await config_entry.runtime_data.async_refresh()
    await run_for(hass, freezer, 20, every=2)
    assert deep_refreshes(mock_client) >= 1
