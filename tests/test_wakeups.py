"""Tests that the vehicle is not woken more often than intended.

Plain polls only read what the cloud has cached. The deep refresh while
charging is the one automatic action that wakes the vehicle, so its limits are
pinned down here, through the whole integration with the clock moving.
"""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

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
from custom_components.uconnect.coordinator import LOCATION_REQUEST_STORAGE_KEY
from custom_components.uconnect.extrapolated_soc import SocEstimationState
from custom_components.uconnect.pyuconnect.client import Location
from custom_components.uconnect.wake_guard import (
    AUTO_REFRESH_MAX_PER_DAY,
    AUTO_REFRESH_MAX_WITHOUT_PROGRESS,
    MANUAL_REFRESH_COOLDOWN,
    STORAGE_KEY,
)

from .conftest import VIN


@pytest.fixture(autouse=True)
def fixed_interval(request):
    """Refresh at the configured interval from the start, unless adaptive.

    Most tests here are about the limits, which a step starting at five
    minutes and growing until refreshes bring a new percent would blur.
    """
    if "adaptive" in request.keywords:
        yield
        return
    with patch(
        "custom_components.uconnect.wake_guard.AUTO_REFRESH_FIRST_STEP",
        timedelta(hours=24),
    ):
        yield


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
    """Make the vehicle a charging one that uploads on every deep refresh.

    It gains gain percent per half hour, like a slow charge, whenever it is
    asked: the state of charge follows the time, not the refreshes.
    """
    started = dt_util.utcnow()
    vehicle.charging = True
    vehicle.plugged_in = True
    vehicle.state_of_charge = 10
    vehicle.timestamp_ev_info = dt_util.utcnow()
    # No time-to-full: the charging speed is only known from the gains, so
    # the configured interval is what applies until it is measured
    vehicle.time_to_fully_charge_l1 = None
    vehicle.time_to_fully_charge_l2 = None
    vehicle.time_to_fully_charge_l3 = None

    def command(vin, cmd):
        if cmd.name == "DEEPREFRESH":
            hours = (dt_util.utcnow() - started).total_seconds() / 3600
            # Whole percent, as the vehicle reports it
            vehicle.state_of_charge = min(int(10 + 2 * gain * hours), 99)
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


async def test_automatic_refresh_is_recorded_as_confirmed(
    hass, freezer, config_entry, mock_client, vehicle
):
    from custom_components.uconnect.wake_guard import async_get_wake_guard

    charging_car(mock_client, vehicle)
    await setup(hass, config_entry, 0)
    await run_for(hass, freezer, 30, every=3)

    automatic = [w for w in async_get_wake_guard(hass).wakes(VIN) if w.automatic]
    assert len(automatic) == 1
    assert automatic[0].confirmed


async def test_unconfirmed_automatic_refresh_stays_unconfirmed(
    hass, freezer, config_entry, mock_client, vehicle
):
    from custom_components.uconnect.wake_guard import async_get_wake_guard

    charging_car(mock_client, vehicle)
    mock_client.command_verify.side_effect = None
    mock_client.command_verify.return_value = False
    await setup(hass, config_entry, 0)
    await run_for(hass, freezer, 30, every=3)

    automatic = [w for w in async_get_wake_guard(hass).wakes(VIN) if w.automatic]
    assert automatic
    assert not any(w.confirmed for w in automatic)


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
    # A new percent at every refresh, so only the daily limit applies
    charging_car(mock_client, vehicle, gain=2)
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

    # Locked by hand a minute before the refresh would be due: it waits the
    # spacing after that command
    await hass.services.async_call(DOMAIN, "doors_lock", {}, blocking=True)
    await run_for(hass, freezer, 4)
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


# --- The first refresh of a charge -------------------------------------


@pytest.mark.parametrize("interval", [0, 30])
async def test_charge_reported_long_ago_does_not_wake(
    hass, freezer, config_entry, mock_client, vehicle, interval
):
    # The cloud says charging, but the vehicle last said so two hours ago:
    # the charge may have ended without the end reaching the cloud
    charging_car(mock_client, vehicle)
    vehicle.timestamp_ev_info = dt_util.utcnow() - timedelta(hours=2)
    await setup(hass, config_entry, interval)

    await run_for(hass, freezer, 6 * 60, every=3)
    assert deep_refreshes(mock_client) == 0


async def test_charge_reported_recently_is_refreshed(
    hass, freezer, config_entry, mock_client, vehicle
):
    charging_car(mock_client, vehicle)
    vehicle.timestamp_ev_info = dt_util.utcnow() - timedelta(minutes=30)
    await setup(hass, config_entry, 0)

    await run_for(hass, freezer, 60, every=3)
    assert deep_refreshes(mock_client) == 1


async def test_no_wake_without_a_time_for_the_charge_data(
    hass, freezer, config_entry, mock_client, vehicle
):
    charging_car(mock_client, vehicle)
    vehicle.timestamp_ev_info = None
    await setup(hass, config_entry, 30)

    await run_for(hass, freezer, 3 * 60, every=3)
    assert deep_refreshes(mock_client) == 0


async def test_vehicle_missing_for_a_moment_is_refreshed_later(
    hass, freezer, config_entry, mock_client, vehicle
):
    charging_car(mock_client, vehicle)
    await setup(hass, config_entry, 0)

    # Not in the account list when the refresh is due
    mock_client.vehicles.clear()
    await run_for(hass, freezer, 20)
    assert deep_refreshes(mock_client) == 0

    mock_client.vehicles[VIN] = vehicle
    vehicle.timestamp_ev_info = dt_util.utcnow()
    await config_entry.runtime_data.async_refresh()
    await run_for(hass, freezer, 20)
    assert deep_refreshes(mock_client) == 1


async def test_refresh_by_hand_at_the_start_is_the_start_refresh(
    hass, freezer, config_entry, mock_client, vehicle
):
    vehicle.charging = False
    await setup(hass, config_entry, 0)
    await run_for(hass, freezer, 30, every=2)

    # Plugged in, and a deep refresh by hand shows the charge
    def command(vin, cmd):
        if cmd.name == "DEEPREFRESH":
            charging_car(mock_client, vehicle)
        return True

    mock_client.command_verify.side_effect = command
    await hass.services.async_call(DOMAIN, "deep_refresh", {}, blocking=True)
    assert deep_refreshes(mock_client) == 1

    await run_for(hass, freezer, 3 * 60, every=3)
    assert deep_refreshes(mock_client) == 1


async def test_rejected_pin_keeps_the_start_refresh_for_later(
    hass, freezer, config_entry, mock_client, vehicle
):
    from custom_components.uconnect.pyuconnect.api import PinRejectedError

    charging_car(mock_client, vehicle)
    mock_client.command_verify.side_effect = PinRejectedError("the PIN was rejected")
    await setup(hass, config_entry, 0)
    await run_for(hass, freezer, 60, every=3)
    # One try, which only reached the PIN check, and none after it
    assert mock_client.command_verify.call_count == 1
    guard = hass.data[DOMAIN]["_wake_guard"]
    assert guard.wakes(VIN) == []

    # With the PIN corrected the refresh at the start still comes
    charging_car(mock_client, vehicle)
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, "pin": "5678"}
    )
    await hass.async_block_till_done()
    await run_for(hass, freezer, 20)
    assert deep_refreshes(mock_client) == 2


# --- How often while charging --------------------------------------------


@pytest.mark.adaptive
async def test_fast_charge_is_refreshed_every_five_minutes(
    hass, freezer, config_entry, mock_client, vehicle
):
    # A DC charge: a new percent every couple of minutes
    charging_car(mock_client, vehicle, gain=15)
    await setup(hass, config_entry, 30)

    await run_for(hass, freezer, 15)
    start = deep_refreshes(mock_client)
    await run_for(hass, freezer, 30)
    assert deep_refreshes(mock_client) - start == 6


@pytest.mark.adaptive
async def test_slow_charge_settles_on_longer_steps(
    hass, freezer, config_entry, mock_client, vehicle
):
    # A domestic socket: about one percent an hour
    charging_car(mock_client, vehicle, gain=0.5)
    await setup(hass, config_entry, 30)

    await run_for(hass, freezer, 3 * 60)
    # 5, 10, 20 minutes, then every 30: far fewer than every five minutes
    assert 4 <= deep_refreshes(mock_client) <= 9


async def test_refused_automatic_refresh_holds_nothing_back(
    hass, freezer, config_entry, mock_client, vehicle
):
    from .test_command_safety import refused_by_the_cloud

    charging_car(mock_client, vehicle)
    mock_client.command_verify.side_effect = refused_by_the_cloud(400)
    await setup(hass, config_entry, 15)
    await run_for(hass, freezer, 20, every=2)
    tried = mock_client.command_verify.call_count
    assert tried >= 1

    # Nothing reached the vehicle: no wake-up kept, a refresh by hand goes out
    assert hass.data[DOMAIN]["_wake_guard"].wakes(VIN) == []
    charging_car(mock_client, vehicle)
    await hass.services.async_call(DOMAIN, "deep_refresh", {}, blocking=True)
    assert mock_client.command_verify.call_count == tried + 1


async def test_charge_reported_before_a_gap_does_not_wake(
    hass, freezer, hass_storage, config_entry, mock_client, vehicle
):
    # Followed every 30 minutes until Home Assistant went down five hours
    # ago; the cloud still says charging, from then
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
                            "at": (now - timedelta(hours=5)).isoformat(),
                            "command": "DEEPREFRESH",
                            "automatic": True,
                        }
                    ],
                    "session": {
                        "started": (now - timedelta(hours=9)).isoformat(),
                        "auto_refreshes": 8,
                    },
                }
            }
        },
    }
    charging_car(mock_client, vehicle)
    vehicle.timestamp_ev_info = now - timedelta(hours=5)
    await setup(hass, config_entry, 30)

    await run_for(hass, freezer, 90, every=3)
    assert deep_refreshes(mock_client) == 0


async def test_refresh_decided_during_a_poll_is_checked_again(
    hass, freezer, config_entry, mock_client, vehicle
):
    charging_car(mock_client, vehicle)
    await setup(hass, config_entry, 0)
    coordinator = config_entry.runtime_data

    # A poll holds the client when the refresh is due, and finds the charge
    # ended
    await coordinator._client_lock.acquire()
    await run_for(hass, freezer, 20)
    vehicle.charging = False
    coordinator._client_lock.release()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert deep_refreshes(mock_client) == 0


def location_requests(mock_client) -> int:
    return sum(
        1
        for call in mock_client.command_verify.call_args_list
        if call.args[1].name == "VF"
    )


async def drive(hass, freezer, config_entry, mock_client, vehicle, minutes: int):
    """Set up with the vehicle on and drive for a while, polling as it goes."""
    vehicle.charging = False
    vehicle.ignition_on = True
    vehicle.location = Location(latitude=48.1, longitude=11.5, updated=dt_util.utcnow())
    await setup(hass, config_entry, 0)
    await run_for(hass, freezer, minutes, every=0.5)


async def test_vehicle_on_is_polled_every_two_minutes(
    hass, freezer, config_entry, mock_client, vehicle
):
    await drive(hass, freezer, config_entry, mock_client, vehicle, 0)
    polls = mock_client.refresh.call_count
    reads = mock_client.get_stolen_vehicle_status.call_count

    await run_for(hass, freezer, 20, every=0.5)

    # Polled more than twice as often as the five minutes set, but the extra
    # data only at the interval set
    assert mock_client.refresh.call_count - polls >= 8
    assert mock_client.get_stolen_vehicle_status.call_count - reads <= 4
    mock_client.command_verify.assert_not_called()

    vehicle.ignition_on = False
    await run_for(hass, freezer, 4, every=0.5)
    assert config_entry.runtime_data.update_interval == timedelta(minutes=5)


async def test_switch_off_is_polled_again_after_one_and_three_minutes(
    hass, freezer, config_entry, mock_client, vehicle
):
    await drive(hass, freezer, config_entry, mock_client, vehicle, 3)
    times = []
    mock_client.refresh.side_effect = lambda: times.append(dt_util.utcnow())

    # The next poll sees the vehicle off, then the interval set applies
    vehicle.ignition_on = False
    await run_for(hass, freezer, 7, every=0.25)
    assert [round((t - times[0]).total_seconds() / 60) for t in times] == [0, 1, 3]
    assert config_entry.runtime_data.update_interval == timedelta(minutes=5)
    mock_client.command_verify.assert_not_called()


async def test_switch_off_polls_stop_on_unload(
    hass, freezer, config_entry, mock_client, vehicle
):
    await drive(hass, freezer, config_entry, mock_client, vehicle, 3)
    vehicle.ignition_on = False
    await run_for(hass, freezer, 2, every=0.5)

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    polls = mock_client.refresh.call_count
    await run_for(hass, freezer, 5, every=0.5)
    assert mock_client.refresh.call_count == polls


async def test_location_is_requested_once_if_it_did_not_come(
    hass, freezer, hass_storage, config_entry, mock_client, vehicle
):
    # Past the startup grace of the wake guard
    await drive(hass, freezer, config_entry, mock_client, vehicle, 16)
    vehicle.ignition_on = False
    mock_client.switched_off_at.return_value = dt_util.utcnow()
    await run_for(hass, freezer, 2.5, every=0.5)
    assert location_requests(mock_client) == 0

    await run_for(hass, freezer, 3, every=0.5)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert location_requests(mock_client) == 1
    assert hass_storage[LOCATION_REQUEST_STORAGE_KEY]["data"] == {
        VIN: mock_client.switched_off_at.return_value.isoformat()
    }

    await run_for(hass, freezer, 60, every=1)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert location_requests(mock_client) == 1


async def test_drive_between_two_polls_is_followed_as_a_switch_off(
    hass, freezer, config_entry, mock_client, vehicle
):
    # Parked, then driven between two polls: the poll after it reads the
    # switch-off, and the states read while on are left out with it
    vehicle.charging = False
    vehicle.ignition_on = False
    vehicle.ev_running = False
    vehicle.location = Location(latitude=48.1, longitude=11.5, updated=dt_util.utcnow())
    await setup(hass, config_entry, 0)
    await run_for(hass, freezer, 16, every=0.5)
    times = []
    mock_client.refresh.side_effect = lambda: times.append(dt_util.utcnow())

    vehicle.ev_running = None
    mock_client.switched_off_at.return_value = dt_util.utcnow()
    await run_for(hass, freezer, 9, every=0.25)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert [round((t - times[0]).total_seconds() / 60) for t in times][:3] == [0, 1, 3]
    assert location_requests(mock_client) == 1


async def test_location_not_read_is_checked_again_with_the_next_poll(
    hass, freezer, config_entry, mock_client, vehicle
):
    await drive(hass, freezer, config_entry, mock_client, vehicle, 16)
    vehicle.ignition_on = False
    # Uploaded with the next poll, which sees it
    mock_client.switched_off_at.return_value = dt_util.utcnow() + timedelta(minutes=2)
    # The cloud failed to answer the location in the polls after it
    mock_client.location_read.return_value = False
    await run_for(hass, freezer, 6, every=0.5)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert location_requests(mock_client) == 0

    # Read at the next poll, still in the window: the old one did not come
    mock_client.location_read.return_value = True
    await run_for(hass, freezer, 6, every=0.5)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert location_requests(mock_client) == 1


async def test_location_never_read_is_not_requested(
    hass, freezer, config_entry, mock_client, vehicle
):
    await drive(hass, freezer, config_entry, mock_client, vehicle, 16)
    vehicle.ignition_on = False
    mock_client.switched_off_at.return_value = dt_util.utcnow()
    mock_client.location_read.return_value = False

    await run_for(hass, freezer, 30, every=0.5)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert location_requests(mock_client) == 0


async def test_location_without_a_time_is_not_requested(
    hass, freezer, config_entry, mock_client, vehicle
):
    await drive(hass, freezer, config_entry, mock_client, vehicle, 16)
    vehicle.ignition_on = False
    mock_client.switched_off_at.return_value = dt_util.utcnow()
    # Read, but the cloud sent no usable time with it
    vehicle.location = Location(latitude=48.2, longitude=11.6, updated=None)

    await run_for(hass, freezer, 30, every=0.5)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert location_requests(mock_client) == 0


async def test_location_that_came_along_is_not_requested(
    hass, freezer, config_entry, mock_client, vehicle
):
    await drive(hass, freezer, config_entry, mock_client, vehicle, 16)
    vehicle.ignition_on = False
    off_at = dt_util.utcnow()
    mock_client.switched_off_at.return_value = off_at
    await run_for(hass, freezer, 1.5, every=0.5)
    # Stamped before the switch-off, as the vehicle does it
    vehicle.location = Location(
        latitude=48.2, longitude=11.6, updated=off_at - timedelta(seconds=85)
    )

    await run_for(hass, freezer, 30, every=0.5)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert location_requests(mock_client) == 0


async def test_location_request_is_not_sent_again_after_a_restart(
    hass, freezer, hass_storage, config_entry, mock_client, vehicle
):
    # Asked for before the restart
    off_at = dt_util.utcnow() + timedelta(minutes=16)
    hass_storage[LOCATION_REQUEST_STORAGE_KEY] = {
        "version": 1,
        "key": LOCATION_REQUEST_STORAGE_KEY,
        "data": {VIN: off_at.isoformat()},
    }
    await drive(hass, freezer, config_entry, mock_client, vehicle, 16)
    vehicle.ignition_on = False
    mock_client.switched_off_at.return_value = off_at

    await run_for(hass, freezer, 30, every=0.5)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert location_requests(mock_client) == 0


async def test_location_is_not_requested_with_a_rejected_pin(
    hass, freezer, config_entry, mock_client, vehicle
):
    await drive(hass, freezer, config_entry, mock_client, vehicle, 16)
    mock_client.api.pin_rejected = True
    vehicle.ignition_on = False
    mock_client.switched_off_at.return_value = dt_util.utcnow()

    await run_for(hass, freezer, 30, every=0.5)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert location_requests(mock_client) == 0


async def test_first_refresh_waiting_for_the_client_needs_fresh_data(
    hass, freezer, config_entry, mock_client, vehicle
):
    # Decided with the charge data 55 minutes old, then held up by a slow
    # poll until it is 80 minutes old: no longer enough for a first refresh
    charging_car(mock_client, vehicle)
    vehicle.timestamp_ev_info = dt_util.utcnow() - timedelta(minutes=40)
    await setup(hass, config_entry, 30)
    coordinator = config_entry.runtime_data

    await run_for(hass, freezer, 10)
    await coordinator._client_lock.acquire()
    await run_for(hass, freezer, 30)
    coordinator._client_lock.release()
    await hass.async_block_till_done(wait_background_tasks=True)

    assert deep_refreshes(mock_client) == 0


async def stop_charging(hass, freezer, vehicle, plugged_in: bool = True) -> None:
    """The cloud reports the charge stopped, the cable in or out."""
    vehicle.charging = False
    vehicle.plugged_in = plugged_in
    vehicle.timestamp_ev_info = dt_util.utcnow()
    await step(hass, freezer, 5.01)


async def test_charge_stopped_with_the_cable_in_is_refreshed_once(
    hass, freezer, config_entry, mock_client, vehicle
):
    """06.11.: reported as stopped right after a level change, while the
    vehicle charged on for 45 minutes without uploading."""
    charging_car(mock_client, vehicle)
    await setup(hass, config_entry, 30)
    await run_for(hass, freezer, 10)
    before = deep_refreshes(mock_client)

    await stop_charging(hass, freezer, vehicle)
    await run_for(hass, freezer, 6)
    assert deep_refreshes(mock_client) == before + 1

    # Only once, also when it stays stopped
    await run_for(hass, freezer, 120, every=5.01)
    assert deep_refreshes(mock_client) == before + 1


@pytest.mark.parametrize("why", ["unplugged", "uploaded", "interval_zero"])
async def test_charge_stopped_is_not_refreshed_when_it_is_clear(
    hass, freezer, config_entry, mock_client, vehicle, why
):
    charging_car(mock_client, vehicle)
    await setup(hass, config_entry, 0 if why == "interval_zero" else 30)
    await run_for(hass, freezer, 10)
    before = deep_refreshes(mock_client)

    await stop_charging(hass, freezer, vehicle, plugged_in=why != "unplugged")
    if why == "uploaded":
        # The vehicle reported again by itself after the stop
        vehicle.timestamp_ev_info = dt_util.utcnow()
    await run_for(hass, freezer, 30)
    assert deep_refreshes(mock_client) == before
