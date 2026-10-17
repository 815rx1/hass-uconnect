"""Coordinator for Uconnect integration"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import dataclasses
from datetime import timedelta
import logging
from typing import Any, TypeVar

from .pyuconnect import Client
from .pyuconnect.command import Command
from .pyuconnect.api import CHARGING_LEVELS_BY_NAME
from .pyuconnect.brands import BRANDS as BRANDS_BY_NAME

from .pyuconnect.api import AuthenticationError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_PASSWORD,
    CONF_PIN,
    CONF_SCAN_INTERVAL,
    CONF_USERNAME,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .charge_schedule import WEEKDAYS, supports_emea_schedules
from .const import (
    CONF_BRAND_REGION,
    CONF_CHARGING_REFRESH_INTERVAL,
    CONF_DISABLE_TLS_VERIFICATION,
    BRANDS,
    DEFAULT_CHARGING_REFRESH_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    MAX_CONSECUTIVE_FAILURES,
    MIN_CHARGING_REFRESH_INTERVAL,
    SLOW_EXTRA_DATA_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")


def is_auth_error(err: BaseException) -> bool:
    """Return whether the error means the stored credentials were rejected.

    Network errors and rate limits must not be mistaken for bad credentials,
    because that would ask the user to log in again for no reason, so only
    the library's dedicated error counts.
    """

    return isinstance(err, AuthenticationError)


class UconnectRequestError(HomeAssistantError):
    """A vehicle action could not be sent or its outcome is unknown.

    Unlike a plain HomeAssistantError, which means the vehicle answered and
    refused, the cloud may still carry this action out, and no refresh has
    followed it yet.
    """


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

            # Only after a successful poll: each of these calls can take until
            # the request timeout while the cloud is down, and they hold the
            # client lock, so commands would queue behind them for minutes
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

            # EMEA vehicles report their schedules inline, and the v4 schedule
            # endpoint only answers HTTP 500 for them
            if not supports_emea_schedules(self.client, self.client.vehicles[vin]):
                try:
                    self.charge_schedule_data[vin] = await self.async_run(
                        self.client.get_charge_schedules, vin
                    )
                except Exception as err:
                    _LOGGER.debug(
                        "Charge schedules not available for %s: %s", vin, err
                    )

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

        await self._async_vehicle_action(
            "Set charge schedule", self.client.set_charge_schedule_verify, vin, schedule
        )

    async def async_set_emea_charge_schedule(
        self, vin: str, slot: int, **changes: Any
    ) -> None:
        """Change one inline charge schedule slot of an EMEA vehicle.

        The backend replaces the whole list on every write, so the other slots
        are sent back unchanged. Days are given as a list of weekday names.
        """

        vehicle = self.client.vehicles.get(vin)
        schedules = list(getattr(vehicle, "charge_schedules", None) or [])
        if not 0 <= slot < len(schedules):
            raise HomeAssistantError(f"Charge schedule {slot + 1} does not exist")

        current = schedules[slot]
        if (days := changes.pop("days", None)) is not None:
            changes["days"] = dataclasses.replace(
                current.days, **{day: day in days for day in WEEKDAYS}
            )
        schedules[slot] = dataclasses.replace(current, **changes)

        await self._async_vehicle_action(
            "Set charge schedule",
            self.client.set_charge_schedule_emea,
            vin,
            schedules,
            True,
        )

    async def async_command(self, vin: str, cmd: Command) -> None:
        """Execute the given command"""

        await self._async_vehicle_action(
            f"Command {cmd.name}", self.client.command_verify, vin, cmd
        )

    async def async_set_charging_level(self, vin: str, level: str) -> None:
        """Set the charging level"""

        if level not in CHARGING_LEVELS_BY_NAME:
            raise ServiceValidationError(f"Invalid charging level: {level}")

        await self._async_vehicle_action(
            "Set charging level",
            self.client.set_charging_level_verify,
            vin,
            CHARGING_LEVELS_BY_NAME[level],
        )

    async def _async_vehicle_action(
        self, what: str, func: Callable[..., Any], *args: Any
    ) -> None:
        """Run a vehicle action, refresh afterwards and report the outcome.

        Errors from the library are turned into UconnectRequestError so the
        UI shows the reason instead of "Unknown error". A falsy result means
        the vehicle refused, which is reported only after the refresh so the
        entities show what the vehicle really did.
        """

        try:
            result = await self.async_run(func, *args)
        except HomeAssistantError:
            raise
        except Exception as err:
            raise UconnectRequestError(f"{what} failed: {err}") from err

        await self.async_refresh()

        if not result:
            raise HomeAssistantError(f"{what} was not confirmed by the vehicle")

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
