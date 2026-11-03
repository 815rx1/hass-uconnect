"""Tests for what the battery estimate learns from the readings it gets.

Taken over from upstream 1.0.3 (hass-uconnect#138): parked readings are
judged against the last reported one, a charge that ended unseen lets the
next reading through, drain samples count by the time they cover, and the
reading that ends a charge still teaches the correction factor.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import State
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    mock_restore_cache_with_extra_data,
)

from custom_components.uconnect.const import DOMAIN
from custom_components.uconnect.extrapolated_soc import (
    DEFAULT_IDLE_DRAIN_RATE,
    IDLE_DRAIN_TIME_CONSTANT_HOURS,
    MAX_IDLE_DRAIN_RATE,
    SocEstimationState,
)
from custom_components.uconnect.wake_guard import (
    AUTO_REFRESH_MAX_WITHOUT_PROGRESS,
    async_get_wake_guard,
)

from .conftest import VIN


def entity_id(hass, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{DOMAIN}_{VIN}_{key}"
    )


@pytest.fixture
def sensor(setup_integration):
    return setup_integration.runtime_data.extrapolated_soc_sensors[VIN]


async def report(hass, freezer, setup_integration, vehicle, hours: float, **values):
    """Let time pass, then poll a status with the values given."""
    freezer.tick(timedelta(hours=hours))
    for name, value in values.items():
        setattr(vehicle, name, value)
    await setup_integration.runtime_data.async_refresh()
    await hass.async_block_till_done()


@pytest.fixture
async def parked(hass, freezer, setup_integration, vehicle, sensor):
    """The vehicle parked at 60 percent, its last report."""
    vehicle.time_to_fully_charge_l1 = None
    vehicle.time_to_fully_charge_l2 = None
    vehicle.time_to_fully_charge_l3 = None
    await report(
        hass, freezer, setup_integration, vehicle, 0,
        charging=False, ignition_on=False, state_of_charge=60,
    )
    assert sensor._state.is_idle
    assert sensor._state.last_vehicle_soc == 60
    return sensor


async def test_lower_parked_reading_above_the_estimate_is_taken(
    hass, freezer, setup_integration, vehicle, parked
):
    # A drain learned too high puts the estimate below what the car says
    parked._state.idle_drain_rate_pct_per_hour = MAX_IDLE_DRAIN_RATE
    await report(hass, freezer, setup_integration, vehicle, 100, state_of_charge=59)

    assert parked.native_value is not None
    assert parked._state.last_vehicle_soc == 59


async def test_higher_parked_reading_is_stale(
    hass, freezer, setup_integration, vehicle, parked
):
    await report(hass, freezer, setup_integration, vehicle, 5, state_of_charge=61)

    assert parked._state.last_vehicle_soc == 60
    assert not parked._state.charged_since_reading


async def test_higher_reading_after_an_unseen_charge_is_taken(
    hass, freezer, setup_integration, vehicle, parked
):
    # The charge came and went without a new state of charge
    await report(hass, freezer, setup_integration, vehicle, 1, charging=True)
    await report(hass, freezer, setup_integration, vehicle, 3, charging=False)
    assert parked._state.charged_since_reading

    await report(hass, freezer, setup_integration, vehicle, 1, state_of_charge=80)

    assert parked._state.last_vehicle_soc == 80
    assert not parked._state.charged_since_reading


async def test_reading_after_a_reported_zero_is_taken(
    hass, freezer, setup_integration, vehicle, parked
):
    # A 0 that got through, e.g. stored by a version without the filter
    await report(hass, freezer, setup_integration, vehicle, 1, ignition_on=True)
    await report(
        hass, freezer, setup_integration, vehicle, 0.1,
        ignition_on=False, state_of_charge=0,
    )
    assert parked._state.last_vehicle_soc == 0

    await report(hass, freezer, setup_integration, vehicle, 1, state_of_charge=75)

    assert parked._state.last_vehicle_soc == 75
    assert parked.native_value == pytest.approx(75, abs=0.1)


async def test_no_drain_is_learned_over_a_charge(
    hass, freezer, setup_integration, vehicle, parked
):
    await report(hass, freezer, setup_integration, vehicle, 1, charging=True)
    await report(hass, freezer, setup_integration, vehicle, 3, charging=False)
    drain = parked._state.idle_drain_rate_pct_per_hour

    await report(hass, freezer, setup_integration, vehicle, 10, state_of_charge=59)

    assert parked._state.last_vehicle_soc == 59
    assert parked._state.idle_drain_rate_pct_per_hour == drain


async def test_drain_sample_counts_by_the_time_it_covers(
    hass, freezer, setup_integration, vehicle, parked
):
    await report(hass, freezer, setup_integration, vehicle, 24, state_of_charge=59)

    observed = min(1 / 24, MAX_IDLE_DRAIN_RATE)
    weight = 24 / (24 + IDLE_DRAIN_TIME_CONSTANT_HOURS)
    expected = DEFAULT_IDLE_DRAIN_RATE + weight * (observed - DEFAULT_IDLE_DRAIN_RATE)
    assert parked._state.idle_drain_rate_pct_per_hour == pytest.approx(expected)


async def test_short_drain_window_moves_the_rate_little(
    hass, freezer, setup_integration, vehicle, parked
):
    # One percent step caught after two hours reads as twenty times the cap
    await report(hass, freezer, setup_integration, vehicle, 2, state_of_charge=59)

    assert parked._state.idle_drain_rate_pct_per_hour < 0.01


async def test_charge_from_parked_waits_for_a_charging_reading(
    hass, freezer, setup_integration, vehicle, parked
):
    await report(hass, freezer, setup_integration, vehicle, 5, charging=True)

    assert parked._state.is_charging
    assert parked._state.rate_baseline_stale


async def test_reading_that_ends_a_charge_teaches_the_correction(
    hass, freezer, setup_integration, vehicle, parked
):
    # The car estimates 8 hours for the 40 percent left: 5 percent an hour
    await report(
        hass, freezer, setup_integration, vehicle, 1,
        charging=True, time_to_fully_charge_l1=480,
        time_to_fully_charge_l2=480, time_to_fully_charge_l3=480,
    )
    assert parked._state.rate_baseline_stale
    assert not parked._state.has_measured_rate
    assert parked._state.charging_rate_pct_per_hour == pytest.approx(5)

    # Over two hours it gained 20 percent, twice the estimate
    await report(
        hass, freezer, setup_integration, vehicle, 2,
        charging=False, state_of_charge=80,
    )

    assert parked._state.learned_correction_factor == pytest.approx(0.3 * 1.5 + 0.7)


async def test_estimate_unavailable_once_the_vehicle_is_gone(
    hass, freezer, setup_integration, mock_client, vehicle, parked
):
    soc_id = entity_id(hass, "extrapolated_soc")
    rate_id = entity_id(hass, "charging_rate")
    assert hass.states.get(soc_id).state != STATE_UNAVAILABLE
    assert hass.states.get(rate_id).state != STATE_UNAVAILABLE

    mock_client.vehicles.clear()
    await report(hass, freezer, setup_integration, vehicle, 1)

    assert hass.states.get(soc_id).state == STATE_UNAVAILABLE
    assert hass.states.get(rate_id).state == STATE_UNAVAILABLE


async def test_estimate_bridges_a_cloud_outage(
    hass, freezer, setup_integration, mock_client, vehicle, parked
):
    soc_id = entity_id(hass, "extrapolated_soc")
    mock_client.refresh.side_effect = Exception("cloud down")
    for _ in range(10):
        await report(hass, freezer, setup_integration, vehicle, 0.1)

    assert not setup_integration.runtime_data.last_update_success
    assert hass.states.get(soc_id).state != STATE_UNAVAILABLE


def test_charged_since_reading_is_kept():
    state = SocEstimationState(charged_since_reading=True)

    assert SocEstimationState.from_dict(state.to_dict()).charged_since_reading
    assert not SocEstimationState.from_dict({}).charged_since_reading


async def test_higher_parked_reading_uploaded_later_is_taken(
    hass, freezer, setup_integration, vehicle, parked
):
    # Charged overnight while Home Assistant was down: no charge was seen,
    # but the vehicle uploaded the reading after the last one
    await report(
        hass, freezer, setup_integration, vehicle, 10,
        state_of_charge=90,
        timestamp_ev_info=vehicle.timestamp_ev_info + timedelta(hours=9),
    )

    assert parked._state.last_vehicle_soc == 90
    assert parked.native_value == pytest.approx(90, abs=0.1)


async def test_lower_charging_reading_after_a_drive_is_a_new_charge(
    hass, freezer, setup_integration, vehicle, sensor
):
    await report(
        hass, freezer, setup_integration, vehicle, 0,
        charging=True, ignition_on=False, state_of_charge=80, odometer=1000,
    )
    guard = async_get_wake_guard(hass)
    session = guard.session(VIN)
    session.without_progress = 3
    session.soc_at_last_refresh = 80

    # The end of that charge and the drive after it went unseen
    await report(
        hass, freezer, setup_integration, vehicle, 8,
        state_of_charge=40, odometer=1050,
    )

    assert sensor._state.last_vehicle_soc == 40
    assert sensor._state.is_charging
    assert sensor.native_value == pytest.approx(40, abs=0.1)
    session = guard.session(VIN)
    assert session.without_progress == 0
    assert session.soc_at_last_refresh is None


async def test_lower_charging_reading_without_a_drive_is_stale(
    hass, freezer, setup_integration, vehicle, sensor
):
    await report(
        hass, freezer, setup_integration, vehicle, 0,
        charging=True, ignition_on=False, state_of_charge=80, odometer=1000,
    )
    await report(hass, freezer, setup_integration, vehicle, 1, state_of_charge=40)

    assert sensor._state.last_vehicle_soc == 80


async def test_charge_seen_ending_full_teaches_no_correction(
    hass, freezer, setup_integration, vehicle, parked
):
    # 5 percent an hour estimated, so full after 8 hours, seen 4 hours late
    await report(
        hass, freezer, setup_integration, vehicle, 1,
        charging=True, time_to_fully_charge_l1=480,
        time_to_fully_charge_l2=480, time_to_fully_charge_l3=480,
    )
    await report(
        hass, freezer, setup_integration, vehicle, 12,
        charging=False, state_of_charge=100,
    )

    assert parked._state.last_vehicle_soc == 100
    assert parked._state.learned_correction_factor == pytest.approx(1.0)


async def test_same_reading_uploaded_again_holds_the_charge_estimate(
    hass, freezer, setup_integration, vehicle, parked
):
    def uploaded(hours: float):
        return dt_util.utcnow() + timedelta(hours=hours)

    await report(
        hass, freezer, setup_integration, vehicle, 1,
        charging=True, timestamp_ev_info=uploaded(1),
    )
    await report(
        hass, freezer, setup_integration, vehicle, 0.5,
        state_of_charge=65, timestamp_ev_info=uploaded(0.5),
    )
    await report(
        hass, freezer, setup_integration, vehicle, 0.1,
        state_of_charge=67, timestamp_ev_info=uploaded(0.1),
    )
    assert parked._state.has_measured_rate
    assert parked._state.charging_rate_pct_per_hour == pytest.approx(20)

    # The charge stalls, the vehicle keeps uploading 67. Coming late, the
    # first repeat may be the old values sent again before the new ones.
    await report(
        hass, freezer, setup_integration, vehicle, 0.5, timestamp_ev_info=uploaded(0.5)
    )
    assert parked.native_value == pytest.approx(77, abs=0.1)
    await report(
        hass, freezer, setup_integration, vehicle, 0.05, timestamp_ev_info=uploaded(0.05)
    )
    assert parked.native_value == pytest.approx(67, abs=0.1)

    # Without a new upload it is extrapolated on from there
    await report(hass, freezer, setup_integration, vehicle, 0.1)
    assert parked.native_value == pytest.approx(69, abs=0.1)
    assert parked._state.charging_rate_pct_per_hour == pytest.approx(20)


async def test_old_values_sent_again_before_the_new_ones_are_passed_over(
    hass, freezer, setup_integration, vehicle, parked
):
    def uploaded(hours: float):
        return dt_util.utcnow() + timedelta(hours=hours)

    await report(
        hass, freezer, setup_integration, vehicle, 1,
        charging=True, timestamp_ev_info=uploaded(1),
    )
    await report(
        hass, freezer, setup_integration, vehicle, 0.5,
        state_of_charge=65, timestamp_ev_info=uploaded(0.5),
    )
    await report(
        hass, freezer, setup_integration, vehicle, 0.1,
        state_of_charge=67, timestamp_ev_info=uploaded(0.1),
    )

    # As on 03.11. at 14:10: a deep refresh after a quarter of an hour first
    # brings the old 67 with a new time, the new reading seconds later
    await report(
        hass, freezer, setup_integration, vehicle, 0.25, timestamp_ev_info=uploaded(0.25)
    )
    assert parked.native_value == pytest.approx(72, abs=0.1)
    await report(
        hass, freezer, setup_integration, vehicle, 0.002,
        state_of_charge=72, timestamp_ev_info=uploaded(0.002),
    )
    assert parked.native_value == pytest.approx(72, abs=0.1)
    assert not parked._state.soc_repeat_held

    # A repeat soon after the reading, the next percent not due yet, holds
    await report(
        hass, freezer, setup_integration, vehicle, 0.05, timestamp_ev_info=uploaded(0.05)
    )
    assert parked.native_value == pytest.approx(72, abs=0.1)


async def test_repeat_passed_over_before_a_pause_is_not_counted_after_it(
    hass, freezer, setup_integration, vehicle, parked
):
    def uploaded(hours: float):
        return dt_util.utcnow() + timedelta(hours=hours)

    await report(
        hass, freezer, setup_integration, vehicle, 1,
        charging=True, timestamp_ev_info=uploaded(1),
    )
    await report(
        hass, freezer, setup_integration, vehicle, 0.5,
        state_of_charge=65, timestamp_ev_info=uploaded(0.5),
    )
    await report(
        hass, freezer, setup_integration, vehicle, 0.1,
        state_of_charge=67, timestamp_ev_info=uploaded(0.1),
    )
    await report(
        hass, freezer, setup_integration, vehicle, 0.25, timestamp_ev_info=uploaded(0.25)
    )
    assert parked._state.soc_repeat_held

    # The charge pauses without a new percent and goes on
    await report(hass, freezer, setup_integration, vehicle, 0.05, charging=False)
    await report(hass, freezer, setup_integration, vehicle, 0.15, charging=True)
    assert not parked._state.soc_repeat_held

    # Old values sent again a quarter of an hour into it are passed over too
    await report(
        hass, freezer, setup_integration, vehicle, 0.25, timestamp_ev_info=uploaded(0.25)
    )
    assert parked.native_value > 70


async def test_stalled_charge_is_held_once_the_refreshes_gave_up(
    hass, freezer, setup_integration, vehicle, parked
):
    def uploaded(hours: float):
        return dt_util.utcnow() + timedelta(hours=hours)

    await report(
        hass, freezer, setup_integration, vehicle, 1,
        charging=True, timestamp_ev_info=uploaded(1),
    )
    await report(
        hass, freezer, setup_integration, vehicle, 0.5,
        state_of_charge=65, timestamp_ev_info=uploaded(0.5),
    )
    await report(
        hass, freezer, setup_integration, vehicle, 0.1,
        state_of_charge=67, timestamp_ev_info=uploaded(0.1),
    )
    await report(
        hass, freezer, setup_integration, vehicle, 0.5, timestamp_ev_info=uploaded(0.5)
    )
    await report(
        hass, freezer, setup_integration, vehicle, 0.05, timestamp_ev_info=uploaded(0.05)
    )
    # The last refreshes brought the same 67 too, and no more are sent
    async_get_wake_guard(hass).session(VIN).without_progress = (
        AUTO_REFRESH_MAX_WITHOUT_PROGRESS
    )

    await report(hass, freezer, setup_integration, vehicle, 3)
    assert parked.native_value == pytest.approx(67, abs=0.1)

    # A new percent from the vehicle is extrapolated on as before
    await report(
        hass, freezer, setup_integration, vehicle, 0.1,
        state_of_charge=68, timestamp_ev_info=uploaded(0),
    )
    assert parked._state.soc_confirmed_time is None


async def test_higher_charging_reading_after_a_drive_is_a_new_charge(
    hass, freezer, setup_integration, vehicle, sensor
):
    vehicle.time_to_fully_charge_l1 = None
    vehicle.time_to_fully_charge_l2 = 120
    vehicle.time_to_fully_charge_l3 = None
    await report(
        hass, freezer, setup_integration, vehicle, 0,
        charging=True, ignition_on=False, state_of_charge=80, odometer=1000,
        charging_level=2,
    )
    guard = async_get_wake_guard(hass)
    session = guard.session(VIN)
    session.without_progress = AUTO_REFRESH_MAX_WITHOUT_PROGRESS

    # Charged on to 95 unseen, driven down to 85 and plugged in again
    await report(
        hass, freezer, setup_integration, vehicle, 8,
        state_of_charge=85, odometer=1050,
    )

    assert guard.session(VIN).without_progress == 0
    assert not sensor._state.measured_this_session
    # Rated from the time-to-full, not from 80 to 85 over the eight hours
    assert sensor._state.charging_rate_pct_per_hour == pytest.approx(7.5)

    # Its next reading belongs to the same charge
    started = guard.session(VIN).started
    await report(hass, freezer, setup_integration, vehicle, 0.5, state_of_charge=88)
    assert guard.session(VIN).started == started
    assert sensor._state.charging_rate_pct_per_hour == pytest.approx(6)


async def test_odometer_arriving_with_the_charge_is_no_new_charge(
    hass, freezer, setup_integration, vehicle, sensor
):
    # As on 30.10.: parked at 52 with the odometer from before the drive,
    # which only came up to date with the upload that started the charge
    await report(
        hass, freezer, setup_integration, vehicle, 0,
        charging=False, ignition_on=False, state_of_charge=52, odometer=32642,
    )
    await report(
        hass, freezer, setup_integration, vehicle, 7,
        charging=True, odometer=32653,
    )
    guard = async_get_wake_guard(hass)
    started = guard.session(VIN).started

    await report(hass, freezer, setup_integration, vehicle, 0.25, state_of_charge=61)

    assert guard.session(VIN).started == started
    assert sensor._state.last_vehicle_soc == 61


async def test_state_is_restored_although_stored_unavailable(
    hass, config_entry, mock_client
):
    state = SocEstimationState(
        last_actual_soc=72,
        last_actual_soc_time=dt_util.utcnow(),
        last_vehicle_soc=72,
        idle_drain_rate_pct_per_hour=0.02,
        learned_correction_factor=1.3,
    )
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State("sensor.fiat_500e_extrapolated_battery", STATE_UNAVAILABLE),
                {"estimation_state": state.to_dict()},
            )
        ],
    )
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    sensor = config_entry.runtime_data.extrapolated_soc_sensors[VIN]
    assert sensor._state.learned_correction_factor == pytest.approx(1.3)
    assert sensor._state.idle_drain_rate_pct_per_hour == pytest.approx(0.02)
    assert sensor.extra_restore_state_data.as_dict() == {
        "estimation_state": sensor._state.to_dict()
    }


def test_upload_times_are_kept():
    at = dt_util.utcnow()
    state = SocEstimationState(
        last_vehicle_soc_uploaded=at, soc_confirmed_time=at, soc_repeat_held=True
    )

    restored = SocEstimationState.from_dict(state.to_dict())
    assert restored.last_vehicle_soc_uploaded == at
    assert restored.soc_confirmed_time == at
    assert restored.soc_repeat_held
