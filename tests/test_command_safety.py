"""Tests that the vehicle is only unlocked or commanded on purpose."""

from __future__ import annotations

import dataclasses
from datetime import timedelta
import time
from unittest.mock import patch

import pytest
from homeassistant.const import EntityCategory
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.uconnect.const import CONF_UNLOCK_REQUIRES_PIN, DOMAIN
from custom_components.uconnect.wake_guard import MANUAL_MAX_PER_HOUR
from custom_components.uconnect.lock import doors_locked
from custom_components.uconnect.pyuconnect.command import COMMAND_DOORS_LOCK

from .conftest import VIN

PIN = "1234"


def sent(mock_client) -> list[str]:
    return [call.args[1].name for call in mock_client.command_verify.call_args_list]


def entity_id(hass, platform: str, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{DOMAIN}_{VIN}_{key}")


async def setup(hass, config_entry, **options) -> None:
    config_entry.add_to_hass(hass)
    if options:
        hass.config_entries.async_update_entry(
            config_entry, options={**config_entry.options, **options}
        )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()


# --- Unlocking needs the PIN --------------------------------------------


async def test_unlock_without_code_is_refused(hass, config_entry, mock_client):
    await setup(hass, config_entry)
    lock = entity_id(hass, "lock", "lock_doors")
    assert hass.states.get(lock).attributes["code_format"] == r"^\d*$"

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("lock", "unlock", {"entity_id": lock}, blocking=True)
    assert "RDU" not in sent(mock_client)


async def test_unlock_with_the_pin(hass, config_entry, mock_client):
    await setup(hass, config_entry)
    lock = entity_id(hass, "lock", "lock_doors")
    await hass.services.async_call(
        "lock", "unlock", {"entity_id": lock, "code": PIN}, blocking=True
    )
    assert sent(mock_client) == ["RDU"]


async def test_lock_needs_no_code(hass, config_entry, mock_client):
    await setup(hass, config_entry)
    lock = entity_id(hass, "lock", "lock_doors")
    await hass.services.async_call("lock", "lock", {"entity_id": lock}, blocking=True)
    assert sent(mock_client) == ["RDL"]


async def test_wrong_codes_pause_unlocking(hass, freezer, config_entry, mock_client):
    await setup(hass, config_entry)
    lock = entity_id(hass, "lock", "lock_doors")
    for _ in range(5):
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call(
                "lock", "unlock", {"entity_id": lock, "code": "0000"}, blocking=True
            )
    # Even the right code is refused for a while now
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            "lock", "unlock", {"entity_id": lock, "code": PIN}, blocking=True
        )
    assert err.value.translation_placeholders == {"minutes": "5"}
    assert "RDU" not in sent(mock_client)

    # The message says how long is left
    with patch(
        "custom_components.uconnect.coordinator.time.monotonic",
        return_value=time.monotonic() + 200,
    ), pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            "lock", "unlock", {"entity_id": lock, "code": PIN}, blocking=True
        )
    assert err.value.translation_placeholders == {"minutes": "2"}


async def test_code_with_spaces_and_as_number(hass, config_entry, mock_client):
    await setup(hass, config_entry)
    lock = entity_id(hass, "lock", "lock_doors")
    await hass.services.async_call(DOMAIN, "doors_unlock", {"code": " 1234 "}, blocking=True)
    await hass.services.async_call("lock", "lock", {"entity_id": lock}, blocking=True)
    await hass.services.async_call(
        "lock", "unlock", {"entity_id": lock, "code": 1234}, blocking=True
    )
    assert sent(mock_client) == ["RDU", "RDL", "RDU"]


async def test_unlock_without_a_pin_set_is_no_wrong_code(hass, config_entry, mock_client):
    mock_client.api.pin = ""
    await setup(hass, config_entry)
    for _ in range(6):
        with pytest.raises(ServiceValidationError) as err:
            await hass.services.async_call(
                DOMAIN, "doors_unlock", {"code": PIN}, blocking=True
            )
        assert err.value.translation_key == "pin_missing"
    assert sent(mock_client) == []


async def test_unlock_all_locks_leaves_the_car_locked(hass, config_entry, mock_client):
    # The usual smoke alarm automation
    await setup(hass, config_entry)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("lock", "unlock", {"entity_id": "all"}, blocking=True)
    assert "RDU" not in sent(mock_client)


async def test_unlock_action_needs_the_pin(hass, config_entry, mock_client):
    await setup(hass, config_entry)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "doors_unlock", {}, blocking=True)
    assert sent(mock_client) == []

    await hass.services.async_call(DOMAIN, "doors_unlock", {"code": PIN}, blocking=True)
    assert sent(mock_client) == ["RDU"]


async def test_pin_requirement_can_be_turned_off(hass, config_entry, mock_client):
    await setup(hass, config_entry, **{CONF_UNLOCK_REQUIRES_PIN: False})
    lock = entity_id(hass, "lock", "lock_doors")
    assert "code_format" not in hass.states.get(lock).attributes

    await hass.services.async_call("lock", "unlock", {"entity_id": lock}, blocking=True)
    assert sent(mock_client) == ["RDU"]


async def test_changing_the_pin_option_reloads(hass, config_entry, mock_client):
    await setup(hass, config_entry)
    lock = entity_id(hass, "lock", "lock_doors")
    hass.config_entries.async_update_entry(
        config_entry,
        options={**config_entry.options, CONF_UNLOCK_REQUIRES_PIN: False},
    )
    await hass.async_block_till_done()
    assert "code_format" not in hass.states.get(lock).attributes


# --- Trunk and doors ----------------------------------------------------


async def test_trunk_is_a_lock_not_a_switch(hass, config_entry, mock_client, vehicle):
    vehicle.supported_commands += ["ROTRUNKLOCK", "ROTRUNKUNLOCK"]
    vehicle.trunk_locked = True
    await setup(hass, config_entry)

    assert entity_id(hass, "switch", "switch_trunk") is None
    trunk = entity_id(hass, "lock", "lock_trunk")
    assert hass.states.get(trunk).state == "locked"

    # Turning off every switch does not open it
    await hass.services.async_call("switch", "turn_off", {"entity_id": "all"}, blocking=True)
    assert "ROTRUNKUNLOCK" not in sent(mock_client)

    await hass.services.async_call(
        "lock", "unlock", {"entity_id": trunk, "code": PIN}, blocking=True
    )
    assert sent(mock_client)[-1] == "ROTRUNKUNLOCK"


def test_doors_locked_looks_at_every_door(vehicle):
    vehicle.door_driver_locked = True
    vehicle.door_passenger_locked = False
    vehicle.door_rear_left_locked = None
    vehicle.door_rear_right_locked = None
    assert doors_locked(vehicle) is False

    vehicle.door_passenger_locked = True
    assert doors_locked(vehicle) is True

    for attr in ("door_driver_locked", "door_passenger_locked"):
        setattr(vehicle, attr, None)
    assert doors_locked(vehicle) is None


# --- Bulk actions do not reach the commands -----------------------------


async def test_area_actions_do_not_command_the_vehicle(
    hass, config_entry, mock_client, vehicle
):
    vehicle.supported_commands += ["REON", "REOFF", "ROHVACON", "ROHVACOFF"]
    assert await async_setup_component(hass, "homeassistant", {})
    await setup(hass, config_entry)
    area = ar.async_get(hass).async_create("Garage")
    devices = dr.async_get(hass)
    device = dr.async_entries_for_config_entry(devices, config_entry.entry_id)[0]
    devices.async_update_device(device.id, area_id=area.id)

    for domain, service in (
        ("homeassistant", "turn_off"),
        ("homeassistant", "turn_on"),
        ("switch", "turn_off"),
        ("switch", "turn_on"),
    ):
        await hass.services.async_call(
            domain, service, {"area_id": area.id}, blocking=True
        )
        await hass.services.async_call(
            domain, service, {"device_id": device.id}, blocking=True
        )

    # The lock is reached by area, but its unlock needs the PIN
    for target in ({"area_id": area.id}, {"device_id": device.id}):
        with pytest.raises(ServiceValidationError):
            await hass.services.async_call("lock", "unlock", target, blocking=True)

    assert sent(mock_client) == []
    mock_client.send_charge_schedules_emea.assert_not_called()


async def test_turning_off_disabled_schedules_sends_nothing(
    hass, config_entry, mock_client
):
    await setup(hass, config_entry)
    for slot in (2, 3):
        await hass.services.async_call(
            "switch",
            "turn_off",
            {"entity_id": entity_id(hass, "switch", f"charge_schedule_{slot}")},
            blocking=True,
        )
    mock_client.send_charge_schedules_emea.assert_not_called()


async def test_known_state_is_not_sent_again(hass, config_entry, mock_client, vehicle):
    vehicle.supported_commands += ["REON", "REOFF", "CNOW"]
    vehicle.ignition_on = True
    vehicle.charging = True
    await setup(hass, config_entry)

    for key in ("switch_engine", "switch_charging"):
        await hass.services.async_call(
            "switch", "turn_on", {"entity_id": entity_id(hass, "switch", key)}, blocking=True
        )
    assert sent(mock_client) == []

    # Turning off is always sent, an out of date state must not keep an
    # engine running
    await hass.services.async_call(
        "switch",
        "turn_off",
        {"entity_id": entity_id(hass, "switch", "switch_engine")},
        blocking=True,
    )
    assert sent(mock_client) == ["REOFF"]


async def test_charging_level_already_set_is_not_sent(hass, config_entry, mock_client):
    await setup(hass, config_entry)
    select = entity_id(hass, "select", "charging_level_preference")
    assert hass.states.get(select).state == "LEVEL_THREE"

    await hass.services.async_call(
        "select", "select_option", {"entity_id": select, "option": "LEVEL_THREE"},
        blocking=True,
    )
    mock_client.set_charging_level_verify.assert_not_called()

    await hass.services.async_call(
        "select", "select_option", {"entity_id": select, "option": "LEVEL_FIVE"},
        blocking=True,
    )
    mock_client.set_charging_level_verify.assert_called_once()


async def test_entity_categories(hass, config_entry, mock_client, vehicle):
    vehicle.supported_commands += ["ROLIGHTS", "CNOW"]
    await setup(hass, config_entry)
    registry = er.async_get(hass)

    def category(platform, key):
        return registry.async_get(entity_id(hass, platform, key)).entity_category

    assert category("switch", "switch_precondition") is EntityCategory.CONFIG
    assert category("switch", "charge_schedule_1") is EntityCategory.CONFIG
    assert category("time", "charge_schedule_1_start_time") is EntityCategory.CONFIG
    assert category("select", "charging_level_preference") is EntityCategory.CONFIG
    assert category("button", "button_deep_refresh") is EntityCategory.DIAGNOSTIC
    assert category("button", "reset_battery_learning") is EntityCategory.CONFIG
    # What a person reaches for stays a primary control
    assert category("lock", "lock_doors") is None
    assert category("button", "button_lights") is None
    assert category("button", "button_charge") is None


async def test_switches_are_taken_out_of_voice_assistants_once(
    hass, config_entry, mock_client
):
    from homeassistant.components.homeassistant.exposed_entities import (
        async_expose_entity,
        async_should_expose,
    )

    assert await async_setup_component(hass, "homeassistant", {})
    # A switch the previous version created and Assist exposed
    config_entry.add_to_hass(hass)
    registry = er.async_get(hass)
    switch = registry.async_get_or_create(
        "switch",
        DOMAIN,
        f"{DOMAIN}_{VIN}_switch_precondition",
        config_entry=config_entry,
    ).entity_id
    async_expose_entity(hass, "conversation", switch, True)

    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert async_should_expose(hass, "conversation", switch) is False

    # Exposed again on purpose, a reload does not undo that
    async_expose_entity(hass, "conversation", switch, True)
    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert async_should_expose(hass, "conversation", switch) is True


# --- The right vehicle --------------------------------------------------


async def test_action_without_device_with_two_known_vehicles(
    hass, config_entry, mock_client
):
    await setup(hass, config_entry)
    # A second account, not loaded right now, with its own car
    other = MockConfigEntry(domain=DOMAIN, unique_id="other", data={}, options={})
    other.add_to_hass(hass)
    dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={(DOMAIN, "ZFAOTHERCAR0000001")}
    )

    with pytest.raises(ServiceValidationError, match="more than one"):
        await hass.services.async_call(DOMAIN, "doors_lock", {}, blocking=True)
    assert sent(mock_client) == []

    # Naming the vehicle works
    device = dr.async_entries_for_config_entry(dr.async_get(hass), config_entry.entry_id)[0]
    await hass.services.async_call(
        DOMAIN, "doors_lock", {"device_id": device.id}, blocking=True
    )
    assert sent(mock_client) == ["RDL"]


async def test_vehicle_missing_from_the_update_is_not_replaced(
    hass, config_entry, mock_client, vehicle
):
    await setup(hass, config_entry)
    # The car is briefly not reported: an action must not go elsewhere or
    # pretend to work
    mock_client.vehicles.clear()
    with pytest.raises(ServiceValidationError, match="not available"):
        await hass.services.async_call(DOMAIN, "doors_lock", {}, blocking=True)
    assert sent(mock_client) == []


# --- Repeats and loops --------------------------------------------------


async def test_locking_always_goes_out(hass, config_entry, mock_client):
    await setup(hass, config_entry)
    lock = entity_id(hass, "lock", "lock_doors")
    await hass.services.async_call("lock", "lock", {"entity_id": lock}, blocking=True)
    await hass.services.async_call(
        "lock", "unlock", {"entity_id": lock, "code": PIN}, blocking=True
    )
    # Locked again right after: it must not stay unlocked
    await hass.services.async_call("lock", "lock", {"entity_id": lock}, blocking=True)
    await hass.services.async_call("lock", "lock", {"entity_id": lock}, blocking=True)
    assert sent(mock_client) == ["RDL", "RDU", "RDL", "RDL"]


async def test_repeated_unlock_is_skipped_quietly(hass, config_entry, mock_client):
    await setup(hass, config_entry)
    await hass.services.async_call(DOMAIN, "doors_unlock", {"code": PIN}, blocking=True)
    # Confirmed moments ago: nothing to send, and no error that would stop
    # the automation calling it
    await hass.services.async_call(DOMAIN, "doors_unlock", {"code": PIN}, blocking=True)
    assert sent(mock_client) == ["RDU"]


async def test_engine_off_after_on(hass, config_entry, mock_client, vehicle):
    vehicle.supported_commands += ["REON", "REOFF"]
    vehicle.ignition_on = False
    await setup(hass, config_entry)
    engine = entity_id(hass, "switch", "switch_engine")
    for service in ("turn_off", "turn_on", "turn_off"):
        await hass.services.async_call(
            "switch", service, {"entity_id": engine}, blocking=True
        )
    assert sent(mock_client) == ["REOFF", "REON", "REOFF"]


async def test_turning_off_all_switches_does_not_stop_a_script(
    hass, config_entry, mock_client, vehicle
):
    # Charging cannot be stopped remotely, which must not end the script
    # before its next step
    vehicle.supported_commands += ["CNOW"]
    vehicle.charging = True
    await setup(hass, config_entry)
    await hass.services.async_call("switch", "turn_off", {"entity_id": "all"}, blocking=True)
    assert "CNOW" not in sent(mock_client)


async def test_limit_reported_through_the_select(hass, config_entry, mock_client):
    await setup(hass, config_entry)
    for _ in range(MANUAL_MAX_PER_HOUR):
        await hass.services.async_call(DOMAIN, "lights_horn", {}, blocking=True)
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            "select",
            "select_option",
            {
                "entity_id": entity_id(hass, "select", "charging_level_preference"),
                "option": "LEVEL_FIVE",
            },
            blocking=True,
        )
    assert err.value.translation_key == "hourly_limit"


async def test_unconfirmed_lock_can_be_retried(hass, config_entry, mock_client):
    await setup(hass, config_entry)
    mock_client.command_verify.return_value = False
    with pytest.raises(Exception):
        await hass.services.async_call(DOMAIN, "doors_lock", {}, blocking=True)
    mock_client.command_verify.return_value = True
    await hass.services.async_call(DOMAIN, "doors_lock", {}, blocking=True)
    assert sent(mock_client) == ["RDL", "RDL"]


def test_commands_cannot_be_changed():
    with pytest.raises(dataclasses.FrozenInstanceError):
        COMMAND_DOORS_LOCK.name = "RDU"


async def test_rejected_pin_is_reported_and_not_counted(hass, config_entry, mock_client):
    from custom_components.uconnect.pyuconnect.api import PinRejectedError

    await setup(hass, config_entry)
    mock_client.command_verify.side_effect = PinRejectedError("rejected")
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(DOMAIN, "doors_lock", {}, blocking=True)
    assert err.value.translation_key == "pin_rejected"
    # Nothing reached the vehicle, so nothing is counted as a wake-up
    assert hass.data[DOMAIN]["_wake_guard"].wakes(VIN) == []

    # Known rejected: refused without trying
    mock_client.api.pin_rejected = True
    mock_client.command_verify.reset_mock()
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "doors_lock", {}, blocking=True)
    mock_client.command_verify.assert_not_called()


async def test_daily_limit(hass, freezer, config_entry, mock_client):
    from custom_components.uconnect.wake_guard import MANUAL_MAX_PER_DAY

    await setup(hass, config_entry)
    for _ in range(MANUAL_MAX_PER_DAY):
        await hass.services.async_call(DOMAIN, "lights_horn", {}, blocking=True)
        freezer.tick(timedelta(minutes=3))
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(DOMAIN, "lights_horn", {}, blocking=True)
    assert err.value.translation_key == "daily_limit"
    # Locking still goes out
    await hass.services.async_call(DOMAIN, "doors_lock", {}, blocking=True)
    assert sent(mock_client)[-1] == "RDL"


async def test_locking_works_while_the_status_cannot_be_read(
    hass, config_entry, mock_client
):
    await setup(hass, config_entry)
    lock = entity_id(hass, "lock", "lock_doors")
    coordinator = config_entry.runtime_data

    # The status poll fails until the entities would turn unavailable
    mock_client.refresh.side_effect = Exception("status down")
    for _ in range(3):
        await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not coordinator.last_update_success
    # The state is not known, the lock can still be used
    assert hass.states.get(lock).state == "unknown"

    await hass.services.async_call("lock", "lock", {"entity_id": lock}, blocking=True)
    assert sent(mock_client) == ["RDL"]


async def test_command_refused_by_the_cloud_holds_nothing_back(
    hass, config_entry, mock_client
):
    import requests

    await setup(hass, config_entry)
    refused = requests.Response()
    refused.status_code = 400
    mock_client.command_verify.side_effect = requests.HTTPError("400", response=refused)
    with pytest.raises(Exception):
        await hass.services.async_call(DOMAIN, "deep_refresh", {}, blocking=True)
    # It never reached the vehicle: no wake-up, no cooldown
    assert hass.data[DOMAIN]["_wake_guard"].wakes(VIN) == []

    mock_client.command_verify.side_effect = None
    mock_client.command_verify.return_value = True
    await hass.services.async_call(DOMAIN, "deep_refresh", {}, blocking=True)
    assert sent(mock_client)[-1] == "DEEPREFRESH"


def refused_by_the_cloud(status: int):
    import requests

    answer = requests.Response()
    answer.status_code = status
    return requests.HTTPError(str(status), response=answer)


async def test_refresh_refused_while_unplugged_says_so(
    hass, config_entry, mock_client, vehicle
):
    from custom_components.uconnect.coordinator import RefreshNeedsPlug

    await setup(hass, config_entry)
    vehicle.plugged_in = False
    mock_client.command_verify.side_effect = refused_by_the_cloud(400)

    with pytest.raises(RefreshNeedsPlug) as raised:
        await hass.services.async_call(DOMAIN, "deep_refresh", {}, blocking=True)

    assert raised.value.translation_key == "refresh_needs_plug"
    assert "plugged in" in str(raised.value)
    assert hass.data[DOMAIN]["_wake_guard"].wakes(VIN) == []


@pytest.mark.parametrize(
    ("plugged_in", "status", "service"),
    [
        (True, 400, "deep_refresh"),
        (None, 400, "deep_refresh"),
        (False, 500, "deep_refresh"),
        (False, 400, "doors_lock"),
    ],
)
async def test_other_refusals_keep_the_reason(
    hass, config_entry, mock_client, vehicle, plugged_in, status, service
):
    from custom_components.uconnect.coordinator import (
        RefreshNeedsPlug,
        UconnectRequestError,
    )

    await setup(hass, config_entry)
    vehicle.plugged_in = plugged_in
    mock_client.command_verify.side_effect = refused_by_the_cloud(status)

    with pytest.raises(UconnectRequestError) as raised:
        await hass.services.async_call(DOMAIN, service, {}, blocking=True)

    assert not isinstance(raised.value, RefreshNeedsPlug)
    assert str(status) in str(raised.value)
