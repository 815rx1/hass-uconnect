"""Coordinator for Uconnect integration"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
import logging
import re
from typing import Any, TypeVar

from requests.adapters import HTTPAdapter

from py_uconnect import Client
from py_uconnect.command import Command
from py_uconnect.api import CHARGING_LEVELS_BY_NAME
from py_uconnect.brands import BRANDS as BRANDS_BY_NAME

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_PIN,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, HomeAssistantError
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .const import (
    AUTH_ERROR_CODES,
    CONF_BRAND_REGION,
    CONF_CHARGING_REFRESH_INTERVAL,
    CONF_DISABLE_TLS_VERIFICATION,
    BRANDS,
    DEFAULT_CHARGING_REFRESH_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    MAX_CONSECUTIVE_FAILURES,
    MIN_CHARGING_REFRESH_INTERVAL,
    REQUEST_TIMEOUT,
    SLOW_EXTRA_DATA_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(DOMAIN)

_T = TypeVar("_T")

_ERROR_CODE_RE = re.compile(r"['\"]errorCode['\"]\s*:\s*(\d+)")


class _TimeoutHTTPAdapter(HTTPAdapter):
    """HTTP adapter that applies a timeout to requests made without one.

    The library sends every request without a timeout, so a cloud endpoint
    that stops answering holds an executor thread forever and the periodic
    update never runs again until the integration is reloaded.
    """

    def send(self, request, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = REQUEST_TIMEOUT
        return super().send(request, **kwargs)


def is_auth_error(err: BaseException) -> bool:
    """Return whether the error means the stored credentials were rejected.

    The library raises plain exceptions carrying the login response, so the
    error code of the identity provider is the only reliable signal. Network
    errors and rate limits must not be mistaken for bad credentials, because
    that would ask the user to log in again for no reason.
    """

    message = str(err)
    if "account login failed" not in message:
        return False

    match = _ERROR_CODE_RE.search(message)
    return match is not None and int(match.group(1)) in AUTH_ERROR_CODES


class UconnectDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        self.platforms: set[str] = set()
        self.extrapolated_soc_sensors: dict = {}
        self.vhr_data: dict[str, dict] = {}
        self.maintenance_data: dict[str, dict] = {}
        self.charge_schedule_data: dict[str, dict] = {}
        self.svla_data: dict[str, dict] = {}

        self.client = Client(
            email=config_entry.data.get(CONF_USERNAME),
            password=config_entry.data.get(CONF_PASSWORD),
            pin=self._read_pin(config_entry),
            brand=BRANDS_BY_NAME[BRANDS[config_entry.data[CONF_BRAND_REGION]]],
            disable_tls_verification=config_entry.data.get(
                CONF_DISABLE_TLS_VERIFICATION
            ),
        )

        adapter = _TimeoutHTTPAdapter()
        self.client.api.sess.mount("https://", adapter)
        self.client.api.sess.mount("http://", adapter)

        # The client shares its token and vehicle objects between calls, so a
        # command and a poll running in two executor threads at once could log
        # in twice or update a vehicle while it is read
        self._client_lock = asyncio.Lock()

        self.consecutive_failures: int = 0
        self.last_successful_update = None
        self._last_slow_extra_update = None

        self.refresh_interval: int = (
            config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL) * 60
        )

        self.charging_refresh_interval: int = self._read_charging_refresh_interval(
            config_entry
        )

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=config_entry,
            update_interval=timedelta(seconds=self.refresh_interval),
            always_update=True,
        )

    @staticmethod
    def _read_pin(config_entry: ConfigEntry) -> str:
        """Return the PIN from the options, falling back to the setup value.

        An empty option means the field was left blank rather than cleared, so
        the PIN given during setup still applies. The API encodes the PIN
        without checking it, so an absent one is returned as an empty string.
        """

        return config_entry.options.get(CONF_PIN) or config_entry.data.get(CONF_PIN, "")

    @staticmethod
    def _read_charging_refresh_interval(config_entry: ConfigEntry) -> int:
        """Return the deep refresh interval while charging, in minutes.

        Zero disables the recurring refresh. Shorter intervals are raised to
        the floor because each deep refresh wakes the vehicle and holds an
        executor thread for as long as the command status poll takes.
        """

        minutes = config_entry.options.get(
            CONF_CHARGING_REFRESH_INTERVAL, DEFAULT_CHARGING_REFRESH_INTERVAL
        )

        if not isinstance(minutes, int) or minutes <= 0:
            return 0

        return max(minutes, MIN_CHARGING_REFRESH_INTERVAL)

    async def async_run(self, func: Callable[..., _T], *args: Any) -> _T:
        """Run a blocking client call in the executor, one call at a time."""

        async with self._client_lock:
            return await self.hass.async_add_executor_job(func, *args)

    async def _async_update_data(self):
        """Update data via library. Called by update_coordinator periodically."""

        try:
            await self.async_run(self.client.refresh)
        except Exception as err:
            if is_auth_error(err):
                raise ConfigEntryAuthFailed(f"Credentials rejected: {err}") from err

            self.consecutive_failures += 1

            # Without any vehicle there is nothing cached to fall back to
            if not self.client.vehicles:
                raise UpdateFailed(f"Initial data fetch failed: {err}") from err

            # A single failed poll is common with this cloud, so the cached
            # data is kept for a while. After that the entities have to turn
            # unavailable rather than show stale values as if they were fresh.
            if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                raise UpdateFailed(
                    f"Update failed {self.consecutive_failures} times in a row: {err}"
                ) from err

            _LOGGER.warning(
                "Update failed (%d/%d), falling back to cached data: %s",
                self.consecutive_failures,
                MAX_CONSECUTIVE_FAILURES,
                err,
            )
        else:
            self.consecutive_failures = 0
            self.last_successful_update = dt_util.utcnow()

        await self._async_update_extra_data()

        return True

    async def _async_update_extra_data(self) -> None:
        """Fetch extra data (VHR, maintenance, schedules, SVLA) for all vehicles.

        The health report and the maintenance history change at most daily,
        so they are fetched on a slow interval instead of on every poll. That
        halves the requests per poll and lowers the risk of rate limiting.
        """

        now = dt_util.utcnow()
        fetch_slow = (
            self._last_slow_extra_update is None
            or now - self._last_slow_extra_update >= SLOW_EXTRA_DATA_INTERVAL
            or any(vin not in self.vhr_data for vin in self.client.vehicles)
        )

        for vin in list(self.client.vehicles):
            if fetch_slow:
                try:
                    self.vhr_data[vin] = await self.async_run(
                        self.client.get_vehicle_health_report, vin
                    )
                except Exception as err:
                    _LOGGER.debug("VHR not available for %s: %s", vin, err)

                try:
                    self.maintenance_data[vin] = await self.async_run(
                        self.client.get_maintenance_history, vin
                    )
                except Exception as err:
                    _LOGGER.debug(
                        "Maintenance history not available for %s: %s", vin, err
                    )

            try:
                self.charge_schedule_data[vin] = await self.async_run(
                    self.client.get_charge_schedules, vin
                )
            except Exception as err:
                _LOGGER.debug("Charge schedules not available for %s: %s", vin, err)

            try:
                self.svla_data[vin] = await self.async_run(
                    self.client.get_stolen_vehicle_status, vin
                )
            except Exception as err:
                _LOGGER.debug(
                    "Stolen vehicle status not available for %s: %s", vin, err
                )

        if fetch_slow:
            self._last_slow_extra_update = now

    async def async_set_charge_schedule(self, vin: str, schedule: dict) -> None:
        """Set a charge schedule"""

        r = await self.async_run(self.client.set_charge_schedule_verify, vin, schedule)
        await self.async_refresh()

        if not r:
            raise HomeAssistantError("Set charge schedule failed")

    async def async_command(self, vin: str, cmd: Command) -> None:
        """Execute the given command"""

        r = await self.async_run(self.client.command_verify, vin, cmd)
        await self.async_refresh()

        if not r:
            raise HomeAssistantError("Command execution failed")

    async def async_set_charging_level(self, vin: str, level: str) -> None:
        """Set the charging level"""

        if level not in CHARGING_LEVELS_BY_NAME:
            raise ValueError(f"Invalid charging level: {level}")
        level = CHARGING_LEVELS_BY_NAME[level]

        r = await self.async_run(self.client.set_charging_level_verify, vin, level)
        await self.async_refresh()

        if not r:
            raise HomeAssistantError("Set charging level failed")

    async def update_options(self, hass: HomeAssistant, config_entry: ConfigEntry):
        self.update_interval = timedelta(
            seconds=config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            * 60
        )
        self.charging_refresh_interval = self._read_charging_refresh_interval(
            config_entry
        )
        self.client.set_pin(self._read_pin(config_entry))

        # A dormant deep refresh schedule has nothing left to re-arm it, so a
        # charge already under way has to be told the options allow one now
        for sensor in self.extrapolated_soc_sensors.values():
            sensor.resume_deep_refresh()
