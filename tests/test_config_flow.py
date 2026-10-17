"""Tests for the config, reauth and options flows."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResultType

from custom_components.uconnect.const import CONF_ADD_COMMAND_ENTITIES, DOMAIN
from custom_components.uconnect.pyuconnect.api import AuthenticationError

USER_INPUT = {
    "username": "user@example.com",
    "password": "secret",
    "brand_region": 1,
    "pin": "1234",
    "disable_tls_verification": False,
}

LOGIN = "custom_components.uconnect.config_flow.API.login"
SETUP = "custom_components.uconnect.async_setup_entry"


async def test_user_flow_creates_entry(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM

    with patch(LOGIN) as login, patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )
        await hass.async_block_till_done()

    assert login.called
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "FIAT_EU user@example.com"
    assert result["data"] == USER_INPUT


async def test_user_flow_invalid_auth(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(LOGIN, side_effect=AuthenticationError("account login failed: x")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_cannot_connect(hass):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(LOGIN, side_effect=Exception("Read timed out")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_duplicate_aborts(hass, config_entry):
    # The fixture entry has a different unique id, create a matching one
    with patch(LOGIN), patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], USER_INPUT
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_reauth_updates_password(hass, config_entry):
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    with patch(LOGIN) as login, patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "new-secret"}
        )
        await hass.async_block_till_done()

    assert login.called
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data["password"] == "new-secret"
    # Untouched fields and the PIN are kept
    assert config_entry.data["username"] == "user@example.com"
    assert config_entry.data["pin"] == "1234"


async def test_reauth_wrong_password_shows_error(hass, config_entry):
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)

    with patch(LOGIN, side_effect=AuthenticationError("account login failed: x")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "wrong"}
        )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"
    assert result["errors"] == {"base": "invalid_auth"}
    assert config_entry.data["password"] == "secret"


async def test_reauth_can_change_pin(hass, config_entry):
    config_entry.add_to_hass(hass)
    result = await config_entry.start_reauth_flow(hass)

    with patch(LOGIN), patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"password": "new-secret", "pin": "9999"}
        )
        await hass.async_block_till_done()

    assert result["reason"] == "reauth_successful"
    assert config_entry.data["pin"] == "9999"


async def test_options_flow(hass, config_entry):
    config_entry.add_to_hass(hass)
    with patch(SETUP, return_value=True):
        result = await hass.config_entries.options.async_init(config_entry.entry_id)
        assert result["type"] is FlowResultType.FORM

        result = await hass.config_entries.options.async_configure(
            result["flow_id"],
            {
                "scan_interval": 10,
                "charging_refresh_interval": 30,
                CONF_ADD_COMMAND_ENTITIES: True,
            },
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options["scan_interval"] == 10
