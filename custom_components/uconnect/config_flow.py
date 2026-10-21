"""Config flow for Uconnect integration."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_PIN,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .pyuconnect.api import API
from .pyuconnect.brands import BRANDS as BRANDS_BY_NAME

from .const import (
    BRANDS,
    CONF_BRAND_REGION,
    CONF_CHARGING_REFRESH_INTERVAL,
    CONF_DISABLE_TLS_VERIFICATION,
    CONF_ADD_COMMAND_ENTITIES,
    CONF_UNLOCK_REQUIRES_PIN,
    DEFAULT_UNLOCK_REQUIRES_PIN,
    DEFAULT_CHARGING_REFRESH_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_PIN,
    DOMAIN,
)
from .coordinator import is_auth_error

_LOGGER = logging.getLogger(__name__)

# Password and PIN are masked in the forms
SECRET = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): SECRET,
        vol.Required(CONF_BRAND_REGION): vol.In(BRANDS),
        vol.Optional(CONF_PIN, default=DEFAULT_PIN): SECRET,
        vol.Required(CONF_DISABLE_TLS_VERIFICATION, default=False): bool,
    }
)

STEP_REAUTH_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): SECRET,
        vol.Optional(CONF_PIN): SECRET,
    }
)

STEP_RECONFIGURE_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PASSWORD): SECRET,
        vol.Optional(CONF_PIN): SECRET,
        vol.Required(CONF_DISABLE_TLS_VERIFICATION): bool,
    }
)

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(
            CONF_SCAN_INTERVAL,
            default=DEFAULT_SCAN_INTERVAL,
        ): vol.All(vol.Coerce(int), vol.Range(min=1, max=999)),
        vol.Optional(CONF_PIN, default=DEFAULT_PIN): SECRET,
        vol.Required(
            CONF_CHARGING_REFRESH_INTERVAL,
            default=DEFAULT_CHARGING_REFRESH_INTERVAL,
        ): vol.All(vol.Coerce(int), vol.Range(min=0, max=1440)),
        vol.Required(CONF_ADD_COMMAND_ENTITIES): bool,
        vol.Required(
            CONF_UNLOCK_REQUIRES_PIN, default=DEFAULT_UNLOCK_REQUIRES_PIN
        ): bool,
    }
)


async def validate_input(hass: HomeAssistant, user_input: dict[str, Any]):
    """Validate the user input allows us to connect."""

    api = API(
        email=user_input[CONF_USERNAME],
        password=user_input[CONF_PASSWORD],
        pin=user_input.get(CONF_PIN, DEFAULT_PIN),
        brand=BRANDS_BY_NAME[BRANDS[user_input[CONF_BRAND_REGION]]],
        disable_tls_verification=user_input.get(CONF_DISABLE_TLS_VERIFICATION, False),
    )

    try:
        await hass.async_add_executor_job(api.login)
    except Exception as err:
        if is_auth_error(err):
            _LOGGER.info("Login rejected: %s", err)
            raise InvalidAuth from err
        _LOGGER.warning("Unable to reach the Uconnect servers: %s", err)
        raise CannotConnect from err


class UconnectOptionFlowHandler(config_entries.OptionsFlow):
    """Handle an option flow for Uconnect"""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle options init setup."""

        if user_input is not None:
            return self.async_create_entry(
                title=self.config_entry.title, data=user_input
            )

        return self.async_show_form(
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA, self.config_entry.options
            ),
        )


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Uconnect"""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry):
        """Initiate options flow instance."""
        return UconnectOptionFlowHandler()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""

        if user_input is None:
            return self.async_show_form(data_schema=STEP_USER_DATA_SCHEMA)

        user_input = {**user_input, CONF_USERNAME: user_input[CONF_USERNAME].strip()}
        errors = {}

        # The unique id is derived from the username as typed, so the same
        # account in other letter case would get a second entry polling and
        # commanding the same vehicles
        account = user_input[CONF_USERNAME].casefold()
        for entry in self._async_current_entries(include_ignore=False):
            if (
                entry.data.get(CONF_BRAND_REGION) == user_input[CONF_BRAND_REGION]
                and str(entry.data.get(CONF_USERNAME, "")).strip().casefold() == account
            ):
                return self.async_abort(reason="already_configured")

        try:
            await validate_input(self.hass, user_input)
        except InvalidAuth:
            errors["base"] = "invalid_auth"
        except CannotConnect:
            errors["base"] = "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected exception")
            errors["base"] = "unknown"
        else:
            title = f"{BRANDS[user_input[CONF_BRAND_REGION]]} {
                user_input[CONF_USERNAME]
            }"
            await self.async_set_unique_id(
                hashlib.sha256(title.encode("utf-8")).hexdigest()
            )
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=title, data=user_input)

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                STEP_USER_DATA_SCHEMA,
                {k: v for k, v in user_input.items() if k != CONF_PASSWORD},
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Start reauth after the stored credentials were rejected."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the new password and log in with it.

        Account and brand stay as they are, because the entry's unique id is
        derived from them and the vehicles belong to that account.
        """
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            data = {**entry.data, CONF_PASSWORD: user_input[CONF_PASSWORD]}
            if user_input.get(CONF_PIN):
                data[CONF_PIN] = user_input[CONF_PIN]

            try:
                await validate_input(self.hass, data)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    entry, data=data, options=_options_with_pin(entry, user_input)
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=STEP_REAUTH_DATA_SCHEMA,
            description_placeholders={"username": entry.data.get(CONF_USERNAME, "")},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change password, PIN or TLS verification of an existing entry.

        Account and brand cannot change here, because the entry's unique id is
        derived from them; a different account is a new entry.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            data = {
                **entry.data,
                CONF_PASSWORD: user_input[CONF_PASSWORD],
                CONF_DISABLE_TLS_VERIFICATION: user_input[
                    CONF_DISABLE_TLS_VERIFICATION
                ],
            }
            if user_input.get(CONF_PIN):
                data[CONF_PIN] = user_input[CONF_PIN]

            try:
                await validate_input(self.hass, data)
            except InvalidAuth:
                errors["base"] = "invalid_auth"
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return self.async_update_reload_and_abort(
                    entry, data=data, options=_options_with_pin(entry, user_input)
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                STEP_RECONFIGURE_DATA_SCHEMA,
                {
                    CONF_DISABLE_TLS_VERIFICATION: entry.data.get(
                        CONF_DISABLE_TLS_VERIFICATION, False
                    )
                },
            ),
            description_placeholders={"username": entry.data.get(CONF_USERNAME, "")},
            errors=errors,
        )


def _options_with_pin(entry: ConfigEntry, user_input: dict[str, Any]) -> dict[str, Any]:
    """Return the options with a newly entered PIN.

    A PIN set in the options wins over the one in the entry data, so a new PIN
    given here would otherwise have no effect.
    """
    options = dict(entry.options)
    if user_input.get(CONF_PIN) and options.get(CONF_PIN):
        options[CONF_PIN] = user_input[CONF_PIN]
    return options


class InvalidAuth(HomeAssistantError):
    """Error to indicate there is invalid auth."""


class CannotConnect(HomeAssistantError):
    """Error to indicate the servers could not be reached."""
