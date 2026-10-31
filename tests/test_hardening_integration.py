"""Tests for the setup, coordinator and frontend fixes of the hardening pass."""

from __future__ import annotations

from datetime import timedelta
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntryState
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.json import JSONEncoder
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.uconnect import frontend_cards
from custom_components.uconnect.const import CONF_UNLOCK_REQUIRES_PIN, DOMAIN
from custom_components.uconnect.diagnostics import async_get_config_entry_diagnostics
from custom_components.uconnect.pyuconnect.api import AuthenticationError

from .conftest import VIN

LOGIN = "custom_components.uconnect.config_flow.API.login"
SETUP = "custom_components.uconnect.async_setup_entry"


# --- Config flows -------------------------------------------------------


@pytest.mark.parametrize("username", ["User@Example.com", "  user@example.com  "])
async def test_same_account_cannot_be_added_twice(hass, config_entry, username):
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(LOGIN) as login:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "username": username,
                "password": "secret",
                "brand_region": 1,
                "pin": "1234",
                "disable_tls_verification": False,
            },
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    # Refused before logging in
    login.assert_not_called()


async def test_new_entry_stores_the_trimmed_username(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(LOGIN), patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {
                "username": " user@example.com ",
                "password": "secret",
                "brand_region": 1,
                "pin": "1234",
                "disable_tls_verification": False,
            },
        )
    assert result["data"]["username"] == "user@example.com"


@pytest.mark.parametrize("source", ["reauth", "reconfigure"])
async def test_new_pin_also_replaces_the_options_pin(hass, config_entry, source):
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry, options={**config_entry.options, "pin": "1111"}
    )
    if source == "reauth":
        result = await config_entry.start_reauth_flow(hass)
        data = {"password": "secret", "pin": "9999"}
    else:
        result = await config_entry.start_reconfigure_flow(hass)
        data = {"password": "secret", "pin": "9999", "disable_tls_verification": False}

    with patch(LOGIN), patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], data)
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert config_entry.data["pin"] == "9999"
    assert config_entry.options["pin"] == "9999"


async def test_options_offer_the_unlock_pin(hass, config_entry):
    config_entry.add_to_hass(hass)
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.options.async_init(config_entry.entry_id)
        schema = result["data_schema"].schema
        key = next(k for k in schema if k == CONF_UNLOCK_REQUIRES_PIN)
        # On unless turned off
        assert key.default() is True


# --- Coordinator --------------------------------------------------------


async def test_rejected_login_later_stops_commands(hass, setup_integration, mock_client):
    coordinator = setup_integration.runtime_data
    mock_client.refresh.side_effect = AuthenticationError("account login failed")

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.auth_failed is True
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)

    with pytest.raises(HomeAssistantError, match="rejected"):
        await hass.services.async_call(DOMAIN, "doors_lock", {}, blocking=True)
    mock_client.command_verify.assert_not_called()


async def test_failing_health_report_is_not_asked_every_poll(
    hass, setup_integration, mock_client
):
    coordinator = setup_integration.runtime_data
    mock_client.get_vehicle_health_report.side_effect = Exception("HTTP 500")
    calls = mock_client.get_vehicle_health_report.call_count

    for _ in range(5):
        await coordinator.async_refresh()
    assert mock_client.get_vehicle_health_report.call_count == calls


async def test_no_v4_schedule_requests_for_combustion_cars(
    hass, config_entry, mock_client, vehicle
):
    vehicle.state_of_charge = None
    vehicle.charge_schedules = []
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    mock_client.get_charge_schedules.assert_not_called()


async def test_no_v4_schedule_requests_for_emea_cars(hass, setup_integration, mock_client):
    mock_client.get_charge_schedules.assert_not_called()


async def test_commands_need_a_pin(hass, setup_integration, mock_client):
    mock_client.api.pin = ""
    with pytest.raises(HomeAssistantError, match="PIN"):
        await hass.services.async_call(DOMAIN, "doors_lock", {}, blocking=True)
    mock_client.command_verify.assert_not_called()


async def test_user_commands_may_bypass_the_login_backoff(
    hass, setup_integration, mock_client
):
    seen = []
    mock_client.command_verify.side_effect = lambda vin, cmd: seen.append(
        mock_client.api.bypass_login_backoff
    ) or True
    await hass.services.async_call(DOMAIN, "doors_lock", {}, blocking=True)
    assert seen == [True]
    # Back to normal afterwards, polling keeps the backoff
    assert mock_client.api.bypass_login_backoff is False


# --- Frontend -----------------------------------------------------------


async def test_card_resource_removed_with_the_last_entry_still_listed(hass, config_entry):
    # Older Home Assistant versions call async_remove_entry while the entry
    # is still listed
    config_entry.add_to_hass(hass)
    with patch.object(
        frontend_cards, "_async_find_lovelace_resource", AsyncMock(return_value="res1")
    ), patch.object(
        frontend_cards, "_async_unregister_lovelace_resource", AsyncMock()
    ) as unregister:
        await frontend_cards.async_remove_frontend_cards_if_last_entry(hass, config_entry)
    unregister.assert_awaited_once_with(hass, "res1")


async def test_card_resource_kept_while_another_entry_exists(hass, config_entry):
    config_entry.add_to_hass(hass)
    MockConfigEntry(domain=DOMAIN, unique_id="other").add_to_hass(hass)
    with patch.object(
        frontend_cards, "_async_unregister_lovelace_resource", AsyncMock()
    ) as unregister:
        await frontend_cards.async_remove_frontend_cards_if_last_entry(hass, config_entry)
    unregister.assert_not_awaited()


async def test_device_of_an_unloaded_entry_can_be_removed(hass, config_entry):
    from custom_components.uconnect import async_remove_config_entry_device

    config_entry.add_to_hass(hass)
    device = SimpleNamespace(identifiers={(DOMAIN, VIN)})
    assert await async_remove_config_entry_device(hass, config_entry, device) is True


# --- Diagnostics --------------------------------------------------------


async def test_diagnostics_show_the_wake_ups(hass, setup_integration, mock_client):
    mock_client._last_status = {}
    await hass.services.async_call(DOMAIN, "doors_lock", {}, blocking=True)
    result = await async_get_config_entry_diagnostics(hass, setup_integration)
    dumped = json.dumps(result, cls=JSONEncoder)

    vehicle = result["vehicles"][0]
    assert [w["command"] for w in vehicle["wake_ups"]] == ["RDL"]
    assert vehicle["wake_ups"][0]["confirmed"] is True
    assert result["coordinator"]["unlock_requires_pin"] is True
    assert VIN not in dumped


async def test_saving_unchanged_options_does_not_reload(hass, setup_integration):
    coordinator = setup_integration.runtime_data
    # The unlock option was never stored, saving the form stores its default
    hass.config_entries.async_update_entry(
        setup_integration,
        options={**setup_integration.options, CONF_UNLOCK_REQUIRES_PIN: True},
    )
    await hass.async_block_till_done()
    assert setup_integration.runtime_data is coordinator


# --- Home Assistant compatibility ---------------------------------------


async def test_reauth_of_a_loaded_entry_reloads_it(
    hass, setup_integration, mock_client, caplog
):
    entry = setup_integration
    coordinator = entry.runtime_data
    result = await entry.start_reauth_flow(hass)
    with patch(LOGIN):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "new-secret"}
        )
        await hass.async_block_till_done()

    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == "new-secret"
    # Set up again with the new login, without the 2026.10 deprecation
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is not coordinator
    assert "update listener" not in caplog.text


async def test_failed_first_poll_with_a_listed_vehicle_retries_setup(
    hass, config_entry, mock_client
):
    # The client lists the vehicle before its status fails
    mock_client.refresh.side_effect = Exception("status timed out")
    config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_display_precision_and_state_classes(hass, setup_integration):
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)

    def entry(key):
        return registry.async_get(
            registry.async_get_entity_id("sensor", DOMAIN, f"{DOMAIN}_{VIN}_{key}")
        )

    # 14.55 V shows as 14.6 V, not 15 V
    assert entry("battery_voltage").options["sensor"]["suggested_display_precision"] == 1
    # The same state classes as the original integration, so switching in
    # either direction keeps the long-term statistics visible
    state = hass.states.get(entry("distance_to_empty").entity_id)
    assert state.attributes["state_class"] == "total"


async def test_numeric_model_reaches_the_device_registry_as_text(
    hass, config_entry, mock_client, vehicle, caplog
):
    from homeassistant.helpers import device_registry as dr

    vehicle.model = 1500
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    device = dr.async_entries_for_config_entry(dr.async_get(hass), config_entry.entry_id)[0]
    assert device.model == "1500"
    # Home Assistant converts it itself for now, but warns that it stops
    assert "non-string" not in caplog.text


async def test_liftgate_sensor(hass, config_entry, mock_client, vehicle):
    vehicle.liftgate_locked = True
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    from homeassistant.helpers import entity_registry as er

    entity_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{DOMAIN}_{VIN}_liftgate_locked"
    )
    # Locked is "off" for a lock binary sensor
    assert hass.states.get(entity_id).state == "off"


async def test_diagnostics_of_an_entry_that_is_not_loaded(hass, config_entry):
    config_entry.add_to_hass(hass)
    result = await async_get_config_entry_diagnostics(hass, config_entry)
    assert result["coordinator"] is None
    assert result["entry"]["data"]["password"] == "**REDACTED**"


async def test_card_is_served_while_the_entry_waits_for_the_cloud(
    hass, config_entry, mock_client
):
    mock_client.refresh.side_effect = Exception("cloud down")
    config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    assert hass.data[DOMAIN][frontend_cards._DATA_KEY] is True


async def test_last_charge_is_kept_over_a_restart(
    hass, hass_storage, config_entry, mock_client
):
    from custom_components.uconnect.coordinator import (
        LAST_CHARGE_SAVE_DELAY,
        LAST_CHARGE_STORAGE_KEY,
    )

    saved = {VIN: {"state_of_charge": 72, "time_to_fully_charge_l1": None,
                   "time_to_fully_charge_l2": 205, "time_to_fully_charge_l3": None,
                   "uploaded": None}}
    hass_storage[LAST_CHARGE_STORAGE_KEY] = {
        "version": 1, "minor_version": 1, "key": LAST_CHARGE_STORAGE_KEY, "data": saved
    }
    mock_client.last_charges.return_value = saved
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    mock_client.restore_last_charges.assert_called_once_with(saved)

    coordinator = config_entry.runtime_data
    with patch.object(coordinator._last_charge._store, "async_delay_save") as save:
        # Nothing new: nothing written
        await coordinator.async_refresh()
        save.assert_not_called()

        newer = {VIN: dict(saved[VIN], state_of_charge=80)}
        mock_client.last_charges.return_value = newer
        await coordinator.async_refresh()
        await coordinator.async_refresh()
        save.assert_called_once()
        assert save.call_args.args[0]() == newer
        assert save.call_args.args[1] == LAST_CHARGE_SAVE_DELAY


async def test_last_charge_reaches_storage(
    hass, hass_storage, freezer, config_entry, mock_client
):
    from custom_components.uconnect.coordinator import (
        LAST_CHARGE_SAVE_DELAY,
        LAST_CHARGE_STORAGE_KEY,
    )

    charge = {VIN: {"state_of_charge": 75, "time_to_fully_charge_l1": None,
                    "time_to_fully_charge_l2": 65, "time_to_fully_charge_l3": 27,
                    "uploaded": "2026-10-28T18:45:33+01:00"}}
    mock_client.last_charges.return_value = charge
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    freezer.tick(LAST_CHARGE_SAVE_DELAY + 1)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass_storage[LAST_CHARGE_STORAGE_KEY]["data"] == charge


async def test_confirmed_states_are_kept_over_a_restart(
    hass, hass_storage, config_entry, mock_client
):
    from custom_components.uconnect.coordinator import CONFIRMED_STATES_STORAGE_KEY

    saved = {VIN: {"lock:x": {"value": {"door_driver_locked": True}, "at": "2026-10-22T20:10:00+00:00"}}}
    hass_storage[CONFIRMED_STATES_STORAGE_KEY] = {
        "version": 1, "minor_version": 1, "key": CONFIRMED_STATES_STORAGE_KEY, "data": saved
    }
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    mock_client.restore_confirmed_states.assert_called_once_with(saved)

    mock_client.confirmed_states.return_value = saved
    await hass.services.async_call(DOMAIN, "doors_lock", {}, blocking=True)
    await hass.async_block_till_done()
    assert hass_storage[CONFIRMED_STATES_STORAGE_KEY]["data"] == saved


async def test_stores_keep_the_vehicles_of_other_entries(
    hass, hass_storage, freezer, config_entry, mock_client
):
    from custom_components.uconnect.coordinator import (
        CONFIRMED_STATES_SAVE_DELAY,
        CONFIRMED_STATES_STORAGE_KEY,
    )

    other = {"ZFAOTHERCAR0000001": {"lock:x": {"value": {"door_driver_locked": True}, "at": "2026-10-22T20:10:00+00:00"}}}
    hass_storage[CONFIRMED_STATES_STORAGE_KEY] = {
        "version": 1, "minor_version": 1, "key": CONFIRMED_STATES_STORAGE_KEY, "data": other
    }
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    mine = {VIN: {"lock:y": {"value": {"door_driver_locked": True}, "at": "2026-10-31T08:00:00+00:00"}}}
    mock_client.confirmed_states.return_value = mine
    await hass.services.async_call(DOMAIN, "doors_lock", {}, blocking=True)
    await hass.async_block_till_done()
    assert hass_storage[CONFIRMED_STATES_STORAGE_KEY]["data"] == {**other, **mine}

    # Replaced by a newer status: dropped after the next poll, while the
    # vehicle of the other entry stays
    mock_client.confirmed_states.return_value = {}
    await config_entry.runtime_data.async_refresh()
    freezer.tick(CONFIRMED_STATES_SAVE_DELAY + 1)
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass_storage[CONFIRMED_STATES_STORAGE_KEY]["data"] == other


async def test_vehicle_store_is_read_once_and_survives_damage(hass, hass_storage):
    from custom_components.uconnect.coordinator import VehicleStore

    key = f"{DOMAIN}.test"
    hass_storage[key] = {"version": 1, "minor_version": 1, "key": key, "data": ["damaged"]}
    store = VehicleStore.shared(hass, key)
    assert VehicleStore.shared(hass, key) is store
    assert await store.async_load() == {}

    assert store.update([VIN], {VIN: {"a": 1}})
    assert not store.update([VIN], {VIN: {"a": 1}})
    # What one entry changed is not lost to another entry loading later
    assert await store.async_load() == {VIN: {"a": 1}}


async def test_rejected_pin_is_kept_over_a_restart(
    hass, hass_storage, config_entry, mock_client
):
    from custom_components.uconnect.coordinator import PIN_REJECTION_STORAGE_KEY

    other = {"pin": "0123456789abcdef", "locked": False}
    mine = {"pin": "fp", "locked": True}
    hass_storage[PIN_REJECTION_STORAGE_KEY] = {
        "version": 1, "minor_version": 1, "key": PIN_REJECTION_STORAGE_KEY,
        "data": {config_entry.entry_id: mine, "other_entry": other},
    }

    def restore(fingerprint, locked, salt):
        mock_client.api.pin_rejected = fingerprint == "fp"

    mock_client.api.restore_pin_rejection.side_effect = restore
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    mock_client.api.restore_pin_rejection.assert_called_once_with(
        "fp", True, config_entry.entry_id
    )
    assert hass_storage[PIN_REJECTION_STORAGE_KEY]["data"] == {
        config_entry.entry_id: mine, "other_entry": other
    }

    # Saving the options unchanged sends it again, e.g. once unlocked in the
    # app. The rejection of the other entry stays.
    mock_client.api.pin_rejected = False
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {**config_entry.options, "pin": "1234"}
    )
    await hass.async_block_till_done()
    mock_client.api.clear_pin_rejection.assert_called_once()
    assert hass_storage[PIN_REJECTION_STORAGE_KEY]["data"] == {"other_entry": other}


async def test_new_pin_in_the_options_does_not_release_the_old_one_early(
    hass, config_entry, mock_client
):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {**config_entry.options, "pin": "5678"}
    )
    await hass.async_block_till_done()
    # The new PIN takes the place of the rejected one when the options apply;
    # released before, the old one could have gone out once more
    mock_client.api.clear_pin_rejection.assert_not_called()
    mock_client.set_pin.assert_called_with("5678")


async def test_rejection_of_a_replaced_pin_is_dropped(
    hass, hass_storage, config_entry, mock_client
):
    from custom_components.uconnect.coordinator import PIN_REJECTION_STORAGE_KEY

    hass_storage[PIN_REJECTION_STORAGE_KEY] = {
        "version": 1, "minor_version": 1, "key": PIN_REJECTION_STORAGE_KEY,
        "data": {config_entry.entry_id: {"pin": "of-the-old-pin", "locked": False}},
    }
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    # The PIN set now was not the one rejected: nothing is held back, and
    # setting the old one again later does not bring the rejection back
    assert not mock_client.api.pin_rejected
    assert hass_storage[PIN_REJECTION_STORAGE_KEY]["data"] == {}


@pytest.mark.parametrize("new_pin", [None, "5678"])
async def test_options_of_an_entry_not_loaded_let_the_pin_go_out(
    hass, hass_storage, config_entry, new_pin
):
    from custom_components.uconnect.const import BRANDS
    from custom_components.uconnect.coordinator import PIN_REJECTION_STORAGE_KEY
    from custom_components.uconnect.pyuconnect.api import _PIN_REJECTIONS, API
    from custom_components.uconnect.pyuconnect.brands import BRANDS as BRANDS_BY_NAME

    stored = {config_entry.entry_id: {"pin": "fp", "locked": True}}
    hass_storage[PIN_REJECTION_STORAGE_KEY] = {
        "version": 1, "minor_version": 1, "key": PIN_REJECTION_STORAGE_KEY, "data": stored,
    }
    brand = BRANDS_BY_NAME[BRANDS[config_entry.data["brand_region"]]]
    API("user@example.com", "pw", "1234", brand)._reject_pin(True)
    assert _PIN_REJECTIONS

    config_entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    options = dict(config_entry.options) | ({"pin": new_pin} if new_pin else {})
    with patch(SETUP, return_value=True):
        await hass.config_entries.options.async_configure(result["flow_id"], options)
        await hass.async_block_till_done()

    if new_pin:
        # Left as it is: the new PIN is not the rejected one, and the stored
        # rejection is dropped at the next setup
        assert _PIN_REJECTIONS
        assert hass_storage[PIN_REJECTION_STORAGE_KEY]["data"] == stored
    else:
        assert not _PIN_REJECTIONS
        assert hass_storage[PIN_REJECTION_STORAGE_KEY]["data"] == {}


# --- Setup while the cloud is not fully there ----------------------------


async def test_entry_starts_without_status_once_its_entities_exist(
    hass, config_entry, mock_client, vehicle
):
    from homeassistant.helpers import entity_registry as er

    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()

    # Home Assistant starts while the status cannot be read: the vehicle is
    # listed, but nothing about it is known
    mock_client.refresh.side_effect = Exception("status timed out")
    for door in ("driver", "passenger", "rear_left", "rear_right"):
        setattr(vehicle, f"door_{door}_locked", None)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    assert config_entry.state is ConfigEntryState.LOADED

    lock = er.async_get(hass).async_get_entity_id(
        "lock", DOMAIN, f"{DOMAIN}_{VIN}_lock_doors"
    )
    assert hass.states.get(lock).state == "unknown"
    # An automation locking the car at night still can
    await hass.services.async_call("lock", "lock", {"entity_id": lock}, blocking=True)
    assert mock_client.command_verify.call_args.args[1].name == "RDL"
    # Once set up, failing polls count as failures again
    assert not config_entry.runtime_data.last_update_success


async def test_account_without_a_vehicle_at_setup_retries(hass, config_entry, mock_client):
    mock_client.vehicles = {}
    mock_client.get_vehicles.return_value = {}
    config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


# --- Entities -------------------------------------------------------------


async def test_update_button_stays_usable_while_polls_fail(
    hass, setup_integration, mock_client
):
    from homeassistant.helpers import entity_registry as er

    coordinator = setup_integration.runtime_data
    button = er.async_get(hass).async_get_entity_id(
        "button", DOMAIN, f"{DOMAIN}_{VIN}_update"
    )
    mock_client.refresh.side_effect = Exception("status down")
    for _ in range(3):
        await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert not coordinator.last_update_success
    assert hass.states.get(button).state != "unavailable"

    mock_client.refresh.side_effect = None
    await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
    assert coordinator.last_update_success


async def test_lock_icons_follow_the_state(hass, config_entry, mock_client, vehicle):
    from homeassistant.helpers import entity_registry as er

    # Reported by the remote status, which the sample does not include
    vehicle.door_driver_locked = True
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)
    door = registry.async_get_entity_id(
        "binary_sensor", DOMAIN, f"{DOMAIN}_{VIN}_door_driver_locked"
    )
    lock = registry.async_get_entity_id("lock", DOMAIN, f"{DOMAIN}_{VIN}_lock_doors")
    coordinator = config_entry.runtime_data

    async def states():
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        return hass.states.get(door), hass.states.get(lock)

    vehicle.door_driver_locked = True
    door_state, _ = await states()
    assert (door_state.state, door_state.attributes["icon"]) == ("off", "mdi:car-door-lock")

    vehicle.door_driver_locked = False
    door_state, _ = await states()
    assert (door_state.state, door_state.attributes["icon"]) == (
        "on", "mdi:car-door-lock-open"
    )

    # Not known: a question mark, neither an open nor a closed lock
    vehicle.door_driver_locked = None
    door_state, _ = await states()
    assert (door_state.state, door_state.attributes["icon"]) == (
        "unknown", "mdi:lock-question"
    )
    mock_client.refresh.side_effect = Exception("status down")
    for _ in range(3):
        _, lock_state = await states()
    assert (lock_state.state, lock_state.attributes["icon"]) == (
        "unknown", "mdi:lock-question"
    )


async def test_sensors_of_states_unknown_at_setup_are_created(
    hass, config_entry, mock_client, vehicle
):
    from homeassistant.helpers import entity_registry as er

    # Reported by the vehicle, but unknown since it was switched off
    vehicle.door_driver_locked = None
    mock_client.reported_parts.return_value = {"door_driver_locked"}
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    door = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{DOMAIN}_{VIN}_door_driver_locked"
    )
    assert door is not None
    assert hass.states.get(door).state == "unknown"
