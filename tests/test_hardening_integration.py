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
