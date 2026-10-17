"""Tests for setup, polling, options and the entities built on top."""

from __future__ import annotations

from datetime import timedelta

import pytest
import voluptuous as vol
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.uconnect.const import CONF_ADD_COMMAND_ENTITIES, DOMAIN
from custom_components.uconnect.pyuconnect.api import AuthenticationError

from .conftest import VIN

PREFIX = "sensor.fiat_500e"


def entity_id(hass, platform: str, key: str) -> str | None:
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{DOMAIN}_{VIN}_{key}")


async def test_setup_creates_entities(hass, setup_integration):
    assert setup_integration.state is ConfigEntryState.LOADED

    odometer = hass.states.get(entity_id(hass, "sensor", "odometer"))
    assert odometer.state == "12345"
    assert odometer.attributes["unit_of_measurement"] == "km"

    # New 500e values parsed from the v3 status
    assert hass.states.get(entity_id(hass, "sensor", "trip_a_distance")).state == "1013"
    assert hass.states.get(entity_id(hass, "sensor", "trip_b_distance")).state == "14"
    assert hass.states.get(entity_id(hass, "sensor", "battery_soh")).state == "97"
    ev_ts = er.async_get(hass).async_get(entity_id(hass, "sensor", "timestamp_ev_info"))
    assert ev_ts.entity_category == "diagnostic"

    # Command entities and the EMEA schedules
    assert entity_id(hass, "lock", "lock_doors") or entity_id(hass, "lock", "doors")
    schedule = hass.states.get(entity_id(hass, "switch", "charge_schedule_1"))
    assert schedule.state == "on"
    assert schedule.attributes["days"] == [
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
    ]
    start = hass.states.get(entity_id(hass, "time", "charge_schedule_1_start_time"))
    assert start.state == "01:00:00"

    # Charging level select maps the integer preference back to a name
    select = hass.states.get(
        entity_id(hass, "select", "charging_level_preference")
    )
    assert select.state == "LEVEL_THREE"


async def test_unload_and_reload(hass, setup_integration, mock_client):
    assert await hass.config_entries.async_reload(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert setup_integration.state is ConfigEntryState.LOADED

    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    await hass.async_block_till_done()
    assert setup_integration.state is ConfigEntryState.NOT_LOADED

    # Actions stay registered so automations keep validating, and explain
    # themselves when nothing is loaded
    assert hass.services.has_service(DOMAIN, "update")
    with pytest.raises(ServiceValidationError, match="not loaded"):
        await hass.services.async_call(DOMAIN, "update", {}, blocking=True)


async def test_setup_retries_when_cloud_down(hass, config_entry, mock_client):
    mock_client.vehicles.clear()
    mock_client.refresh.side_effect = Exception("Read timed out")
    config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_setup_starts_reauth_on_rejected_login(hass, config_entry, mock_client):
    mock_client.refresh.side_effect = AuthenticationError("account login failed: x")
    config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_entities_turn_unavailable_after_repeated_failures(
    hass, setup_integration, mock_client
):
    coordinator = setup_integration.runtime_data
    odometer_id = entity_id(hass, "sensor", "odometer")
    mock_client.refresh.side_effect = Exception("Read timed out")
    calls_before = mock_client.get_vehicle_health_report.call_count

    # Polls are triggered directly: the scheduled ones carry a random offset
    # of up to a second, which makes stepping the clock racy
    for _ in range(2):
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        # Two failures keep the cached values
        assert hass.states.get(odometer_id).state == "12345"

    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(odometer_id).state == STATE_UNAVAILABLE

    # No extra data is requested while the cloud is down
    assert mock_client.get_vehicle_health_report.call_count == calls_before

    # Recovers on the next good poll
    mock_client.refresh.side_effect = None
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(odometer_id).state == "12345"
    assert coordinator.consecutive_failures == 0


async def test_scheduled_polling_runs(hass, setup_integration, mock_client):
    calls = mock_client.refresh.call_count
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=6))
    await hass.async_block_till_done()
    assert mock_client.refresh.call_count == calls + 1


async def test_vehicle_dropping_out_makes_entities_unavailable(
    hass, setup_integration, mock_client, vehicle
):
    coordinator = setup_integration.runtime_data
    odometer_id = entity_id(hass, "sensor", "odometer")
    mock_client.vehicles.clear()

    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(odometer_id).state == STATE_UNAVAILABLE

    mock_client.vehicles[VIN] = vehicle
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(odometer_id).state == "12345"


async def test_disabling_command_entities_removes_them(hass, setup_integration):
    registry = er.async_get(hass)
    switch_id = entity_id(hass, "switch", "charge_schedule_1")
    assert switch_id is not None

    hass.config_entries.async_update_entry(
        setup_integration,
        options={**setup_integration.options, CONF_ADD_COMMAND_ENTITIES: False},
    )
    await hass.async_block_till_done()

    assert setup_integration.state is ConfigEntryState.LOADED
    assert registry.async_get(switch_id) is None
    assert hass.states.get(switch_id) is None
    # The battery learning reset is not a command and stays
    assert entity_id(hass, "button", "reset_battery_learning") is not None
    # Sensors are untouched
    assert hass.states.get(entity_id(hass, "sensor", "odometer")).state == "12345"

    hass.config_entries.async_update_entry(
        setup_integration,
        options={**setup_integration.options, CONF_ADD_COMMAND_ENTITIES: True},
    )
    await hass.async_block_till_done()
    assert hass.states.get(entity_id(hass, "switch", "charge_schedule_1")).state == "on"


async def test_interval_change_applies_without_reload(hass, setup_integration, mock_client):
    coordinator = setup_integration.runtime_data
    hass.config_entries.async_update_entry(
        setup_integration, options={**setup_integration.options, "scan_interval": 15}
    )
    await hass.async_block_till_done()

    # Same coordinator, new interval
    assert setup_integration.runtime_data is coordinator
    assert coordinator.update_interval == timedelta(minutes=15)


async def test_schedule_switch_writes_full_list(hass, setup_integration, mock_client):
    switch_id = entity_id(hass, "switch", "charge_schedule_2")
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": switch_id}, blocking=True
    )

    vin, schedules, verify = mock_client.set_charge_schedule_emea.call_args.args
    assert vin == VIN and verify is True
    assert [s.enable for s in schedules] == [True, True, False]
    # The first slot went back unchanged
    assert schedules[0].start_time == "01:00" and schedules[0].days.monday


async def test_schedule_time_entity(hass, setup_integration, mock_client):
    time_id = entity_id(hass, "time", "charge_schedule_1_end_time")
    await hass.services.async_call(
        "time", "set_value", {"entity_id": time_id, "time": "06:30:00"}, blocking=True
    )

    schedules = mock_client.set_charge_schedule_emea.call_args.args[1]
    assert schedules[0].end_time == "06:30"
    assert schedules[0].start_time == "01:00"


async def test_schedule_slot_service(hass, setup_integration, mock_client):
    device_id = next(iter(er.async_get(hass).entities.values())).device_id
    await hass.services.async_call(
        DOMAIN,
        "set_charge_schedule_slot",
        {
            "device_id": device_id,
            "slot": 3,
            "enabled": True,
            "start_time": "22:15",
            "end_time": "04:00:00",
            "days": ["saturday", "sunday"],
            "charge_to_full": True,
        },
        blocking=True,
    )

    slot = mock_client.set_charge_schedule_emea.call_args.args[1][2]
    assert slot.enable and slot.charge_to_full
    assert (slot.start_time, slot.end_time) == ("22:15", "04:00")
    assert slot.days.saturday and slot.days.sunday and not slot.days.monday


async def test_schedule_slot_service_rejects_bad_input(hass, setup_integration):
    device_id = next(iter(er.async_get(hass).entities.values())).device_id
    for data in (
        {"slot": 1, "days": ["someday"]},
        {"slot": 7, "enabled": True},
        {"slot": 1, "start_time": "25:99"},
    ):
        with pytest.raises(vol.Invalid):
            await hass.services.async_call(
                DOMAIN,
                "set_charge_schedule_slot",
                {"device_id": device_id, **data},
                blocking=True,
            )
    with pytest.raises(ServiceValidationError, match="Nothing to change"):
        await hass.services.async_call(
            DOMAIN,
            "set_charge_schedule_slot",
            {"device_id": device_id, "slot": 1},
            blocking=True,
        )


async def test_failed_command_raises_readable_error(hass, setup_integration, mock_client):
    mock_client.command_verify.side_effect = Exception("HTTP 500")
    lock_id = next(
        e.entity_id
        for e in er.async_get(hass).entities.values()
        if e.domain == "lock" and e.platform == DOMAIN
    )
    with pytest.raises(HomeAssistantError, match="HTTP 500"):
        await hass.services.async_call(
            "lock", "lock", {"entity_id": lock_id}, blocking=True
        )


async def test_refused_command_is_reported(hass, setup_integration, mock_client):
    mock_client.command_verify.return_value = False
    button_id = next(
        e.entity_id
        for e in er.async_get(hass).entities.values()
        if e.domain == "button" and e.unique_id.endswith("button_lights_horn")
    )
    with pytest.raises(HomeAssistantError, match="not confirmed"):
        await hass.services.async_call(
            "button", "press", {"entity_id": button_id}, blocking=True
        )


async def test_reset_battery_learning(hass, setup_integration):
    button_id = entity_id(hass, "button", "reset_battery_learning")
    await hass.services.async_call(
        "button", "press", {"entity_id": button_id}, blocking=True
    )


async def test_reset_battery_learning_without_sensor(hass, setup_integration):
    registry = er.async_get(hass)
    soc_id = next(
        e.entity_id
        for e in registry.entities.values()
        if e.unique_id.endswith("_extrapolated_soc") or "extrapolated" in e.unique_id
    )
    registry.async_update_entity(soc_id, disabled_by=er.RegistryEntryDisabler.USER)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_reload(setup_integration.entry_id)
    await hass.async_block_till_done()

    button_id = entity_id(hass, "button", "reset_battery_learning")
    with pytest.raises(HomeAssistantError, match="disabled"):
        await hass.services.async_call(
            "button", "press", {"entity_id": button_id}, blocking=True
        )


async def test_actions_exist_while_setup_retries(hass, config_entry, mock_client):
    mock_client.vehicles.clear()
    mock_client.refresh.side_effect = Exception("Read timed out")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    for service in ("update", "doors_lock", "set_charge_schedule_slot"):
        assert hass.services.has_service(DOMAIN, service)


async def test_update_action_without_device(hass, setup_integration, mock_client):
    calls = mock_client.refresh.call_count
    await hass.services.async_call(DOMAIN, "update", {}, blocking=True)
    assert mock_client.refresh.call_count == calls + 1


async def test_unsupported_command_is_rejected(hass, setup_integration, mock_client):
    with pytest.raises(ServiceValidationError, match="not supported"):
        await hass.services.async_call(DOMAIN, "engine_on", {}, blocking=True)
    mock_client.command_verify.assert_not_called()


async def test_supported_command_action(hass, setup_integration, mock_client):
    await hass.services.async_call(DOMAIN, "doors_lock", {}, blocking=True)
    assert mock_client.command_verify.call_args.args[1].name == "RDL"


async def test_unknown_device_is_rejected(hass, setup_integration):
    with pytest.raises(ServiceValidationError, match="not found"):
        await hass.services.async_call(
            DOMAIN, "update", {"device_id": "nope"}, blocking=True
        )


async def test_stale_vehicle_device_can_be_removed(hass, setup_integration, mock_client):
    from homeassistant.helpers import device_registry as dr
    from custom_components.uconnect import async_remove_config_entry_device

    device = dr.async_entries_for_config_entry(dr.async_get(hass), setup_integration.entry_id)[0]
    assert device is not None
    assert not await async_remove_config_entry_device(hass, setup_integration, device)

    mock_client.vehicles.clear()
    assert await async_remove_config_entry_device(hass, setup_integration, device)


async def test_image_follows_url_and_sniffs_type(hass, setup_integration, mock_client, vehicle):
    from custom_components.uconnect.image import _sniff_content_type

    assert _sniff_content_type(b"\x89PNG\r\n") == "image/png"
    assert _sniff_content_type(b"\xff\xd8\xff") == "image/jpeg"
    assert _sniff_content_type(b"RIFF\x00\x00\x00\x00WEBPVP8") == "image/webp"

    image_id = entity_id(hass, "image", "image")
    if image_id is None:
        vehicle.image_url = "https://example.invalid/a.png"
        assert await hass.config_entries.async_reload(setup_integration.entry_id)
        await hass.async_block_till_done()
        image_id = entity_id(hass, "image", "image")
    assert image_id is not None
    before = hass.states.get(image_id).state

    vehicle.image_url = "https://example.invalid/b.png"
    await setup_integration.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get(image_id).state != before
