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
from homeassistant.helpers import entity_registry as er

from custom_components.uconnect.const import DOMAIN
from custom_components.uconnect.extrapolated_soc import (
    DEFAULT_IDLE_DRAIN_RATE,
    IDLE_DRAIN_TIME_CONSTANT_HOURS,
    MAX_IDLE_DRAIN_RATE,
    SocEstimationState,
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
