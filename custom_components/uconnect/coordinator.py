"""Coordinator for Uconnect integration"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
import hmac
import logging
import math
import time
from typing import Any, NoReturn, TypeVar

from .pyuconnect import Client
from .pyuconnect.command import Command
from .pyuconnect.api import CHARGING_LEVELS_BY_NAME
from .pyuconnect.brands import BRANDS as BRANDS_BY_NAME

from .pyuconnect.api import (
    AuthenticationError,
    PinAuthError,
    PinRejectedError,
    forget_pin_rejection,
)

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
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
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .charge_schedule import supports_emea_schedules
from .const import (
    CONF_BRAND_REGION,
    CONF_CHARGING_REFRESH_INTERVAL,
    CONF_DISABLE_TLS_VERIFICATION,
    CONF_UNLOCK_REQUIRES_PIN,
    BRANDS,
    DEFAULT_CHARGING_REFRESH_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_UNLOCK_REQUIRES_PIN,
    EVENT_PLACEHOLDER_DISCARDED,
    MAX_CONSECUTIVE_FAILURES,
    MIN_CHARGING_REFRESH_INTERVAL,
    SLOW_EXTRA_DATA_INTERVAL,
    UNLOCK_COMMANDS,
    UNLOCK_LOCKOUT,
    UNLOCK_MAX_WRONG_CODES,
    DOMAIN,
)
from .wake_guard import (
    DEEP_REFRESH_COMMANDS,
    AlreadyDone,
    Blocked,
    async_get_wake_guard,
)

_LOGGER = logging.getLogger(__name__)

_T = TypeVar("_T")

# Name the wake guard records a schedule write under
SCHEDULE_COMMAND = "CPPLUS"

# Changes the vehicle confirmed but has not uploaded yet: lock states, charge
# schedules and the charging level. The vehicle often sends no status after a
# remote lock, so without these a restart would show the doors as they were
# before the lock.
CONFIRMED_STATES_STORAGE_KEY = f"{DOMAIN}.confirmed_states"

# The last state of charge each vehicle really reported. After a restart the
# cloud can answer with a placeholder first and repeat it until the vehicle
# uploads again, see EVENT_PLACEHOLDER_DISCARDED. It changes with every
# upload, so the writes are gathered.
LAST_CHARGE_STORAGE_KEY = f"{DOMAIN}.last_charge"
LAST_CHARGE_SAVE_DELAY = 60
# Confirmed states the cloud has since replaced are dropped from the store
# after a poll; a restart would otherwise bring them back
CONFIRMED_STATES_SAVE_DELAY = 10

# A PIN the backend rejected, per entry, so a restart does not send it again:
# every wrong attempt brings the PIN lock closer, also for the official app
PIN_REJECTION_STORAGE_KEY = f"{DOMAIN}.pin_rejection"

# The placeholder statuses automations were told about, kept per VIN for the
# Home Assistant run so a reload of the entry does not tell them again
_PLACEHOLDERS_TOLD = "_placeholders_told"


class VehicleStore:
    """A store all entries share, each one writing only its own vehicles.

    The data is kept per VIN under one key. An entry saving only what its
    own client knows would drop the vehicles of every other entry.
    """

    def __init__(self, hass: HomeAssistant, key: str) -> None:
        self._store: Store[dict[str, Any]] = Store(hass, 1, key, atomic_writes=True)
        self._data: dict[str, Any] | None = None
        self._lock = asyncio.Lock()

    @classmethod
    def shared(cls, hass: HomeAssistant, key: str) -> VehicleStore:
        return hass.data.setdefault(DOMAIN, {}).setdefault(f"_store_{key}", cls(hass, key))

    async def async_load(self) -> dict[str, Any]:
        """Return what is stored, read once per Home Assistant run."""
        async with self._lock:
            if self._data is None:
                try:
                    data = await self._store.async_load()
                except Exception as err:  # A damaged file must not stop the setup
                    _LOGGER.debug("Unable to read %s: %s", self._store.key, err)
                    data = None
                self._data = dict(data) if isinstance(data, dict) else {}
            return dict(self._data)

    def update(self, vins: Any, entries: dict[str, Any]) -> bool:
        """Take over the entries of these vehicles, return whether any changed."""
        if self._data is None:
            self._data = {}
        changed = False
        for vin in vins:
            entry = entries.get(vin)
            if entry is None:
                changed = self._data.pop(vin, None) is not None or changed
            elif self._data.get(vin) != entry:
                self._data[vin] = entry
                changed = True
        return changed

    def async_delay_save(self, delay: float) -> None:
        self._store.async_delay_save(lambda: dict(self._data or {}), delay)

    async def async_save(self) -> None:
        await self._store.async_save(dict(self._data or {}))


def is_auth_error(err: BaseException) -> bool:
    """Return whether the error means the stored credentials were rejected.

    Network errors and rate limits must not be mistaken for bad credentials,
    because that would ask the user to log in again for no reason, so only
    the library's dedicated error counts.
    """

    return isinstance(err, AuthenticationError)


def _refused_before_queued(err: BaseException) -> bool:
    """Whether a command failed before anything reached the vehicle.

    The cloud refused it outright with a 4xx answer, or the PIN
    authentication before it failed, whatever the answer.
    """

    if isinstance(err, PinAuthError):
        return True
    response = getattr(err, "response", None)
    status = getattr(response, "status_code", None)
    return isinstance(status, int) and 400 <= status < 500


class UconnectRequestError(HomeAssistantError):
    """A vehicle action could not be sent or its outcome is unknown.

    Unlike a plain HomeAssistantError, which means the vehicle answered and
    refused, the cloud may still carry this action out, and no refresh has
    followed it yet.
    """


class RefreshNeedsPlug(UconnectRequestError):
    """A deep refresh the cloud refused while the vehicle is not plugged in.

    The 500e takes it only while plugged in. The cloud refuses it outright
    otherwise, so nothing reached the vehicle; the raw answer, a 400 from
    the fallback variant, told the user nothing.
    """

    def __init__(self) -> None:
        super().__init__(
            "Deep refresh only works while the vehicle is plugged in. The cloud "
            "refused it, the vehicle was not woken.",
            translation_domain=DOMAIN,
            translation_key="refresh_needs_plug",
        )


class PinRejected(ServiceValidationError):
    """The backend rejected the PIN, nothing reached the vehicle.

    The library does not send the PIN again until it is changed, as further
    wrong attempts can lock it, also for the official app.
    """

    def __init__(self, locked: bool = False) -> None:
        if locked:
            super().__init__(
                "The Uconnect PIN is locked. Unlock it in the official app, then "
                "save the integration options once.",
                translation_domain=DOMAIN,
                translation_key="pin_locked",
            )
            return
        super().__init__(
            "The Uconnect PIN was rejected. Correct it in the integration options.",
            translation_domain=DOMAIN,
            translation_key="pin_rejected",
        )


class UconnectDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from the API."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
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
        # The placeholder status of each vehicle automations were told about
        self._placeholders_told: dict[str, dict[str, Any]] = hass.data.setdefault(
            DOMAIN, {}
        ).setdefault(_PLACEHOLDERS_TOLD, {})
        self.last_successful_update = None
        # When the health report and maintenance history were last asked
        # for, per vehicle, whether that worked or not
        self._last_slow_attempt: dict[str, datetime] = {}
        # Set once the login was rejected. Until the entry is set up again
        # with new credentials nothing is sent, every further attempt with
        # the old ones only brings the account closer to a lockout.
        self.auth_failed = False
        # Wrong unlock codes in a row, and until when unlocking is refused
        self._wrong_codes = 0
        self._unlock_refused_until = 0.0

        self.charging_refresh_interval: int = self._read_charging_refresh_interval(
            config_entry
        )
        self._confirmed_states = VehicleStore.shared(hass, CONFIRMED_STATES_STORAGE_KEY)
        self._last_charge = VehicleStore.shared(hass, LAST_CHARGE_STORAGE_KEY)
        self._pin_rejection = VehicleStore.shared(hass, PIN_REJECTION_STORAGE_KEY)

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            config_entry=config_entry,
            update_interval=timedelta(
                minutes=config_entry.options.get(
                    CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                )
            ),
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

        Zero leaves only the refresh at the start of a charge. Shorter
        intervals are raised to the floor because each deep refresh wakes the
        vehicle and holds an executor thread for as long as the command status
        poll takes.
        """

        minutes = config_entry.options.get(
            CONF_CHARGING_REFRESH_INTERVAL, DEFAULT_CHARGING_REFRESH_INTERVAL
        )

        if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes <= 0:
            return 0

        return max(minutes, MIN_CHARGING_REFRESH_INTERVAL)

    @property
    def unlock_requires_pin(self) -> bool:
        """Whether unlocking asks for the Uconnect PIN as its code."""
        return bool(
            self.config_entry.options.get(
                CONF_UNLOCK_REQUIRES_PIN, DEFAULT_UNLOCK_REQUIRES_PIN
            )
        )

    def check_unlock_code(self, code: str | None) -> None:
        """Raise unless the code given with an unlock is the Uconnect PIN.

        Without the code an unlock stays where it came from: an automation
        unlocking "all" locks on a smoke alarm, a scene, a voice assistant,
        an accidental tap. Several wrong codes in a row refuse unlocking for
        a while.
        """
        if not self.unlock_requires_pin:
            return

        # Without a PIN there is nothing to compare with, and no command could
        # be sent anyway. That is no wrong code, so it does not count as one.
        pin = str(self.client.api.pin or "")
        if not pin:
            self._raise_pin_missing()

        if (remaining := self._unlock_refused_until - time.monotonic()) > 0:
            raise ServiceValidationError(
                "Unlocking is paused after several wrong codes.",
                translation_domain=DOMAIN,
                translation_key="unlock_locked_out",
                translation_placeholders={
                    "minutes": str(max(math.ceil(remaining / 60), 1))
                },
            )

        code = "" if code is None else str(code).strip()
        if not code:
            raise ServiceValidationError(
                "Unlocking needs the Uconnect PIN as the code.",
                translation_domain=DOMAIN,
                translation_key="unlock_code_required",
            )

        if not hmac.compare_digest(code.encode(), pin.encode()):
            self._wrong_codes += 1
            if self._wrong_codes >= UNLOCK_MAX_WRONG_CODES:
                self._wrong_codes = 0
                self._unlock_refused_until = (
                    time.monotonic() + UNLOCK_LOCKOUT.total_seconds()
                )
                _LOGGER.warning(
                    "Unlocking refused for %s after %d wrong codes",
                    UNLOCK_LOCKOUT,
                    UNLOCK_MAX_WRONG_CODES,
                )
            raise ServiceValidationError(
                "The code is not the Uconnect PIN.",
                translation_domain=DOMAIN,
                translation_key="unlock_code_wrong",
            )

        self._wrong_codes = 0

    async def async_run(
        self, func: Callable[..., _T], *args: Any, on_request: bool = False
    ) -> _T:
        """Run a blocking client call in the executor, one call at a time."""

        async with self._client_lock:
            return await self._async_run_locked(func, *args, on_request=on_request)

    async def _async_run_locked(
        self, func: Callable[..., _T], *args: Any, on_request: bool = False
    ) -> _T:
        """Run a client call while the client lock is already held.

        Calls made on request may log in although the login is backing off
        after failures: somebody locking the car must not wait for that.
        """

        api = self.client.api
        api.bypass_login_backoff = on_request
        try:
            return await self.hass.async_add_executor_job(func, *args)
        finally:
            api.bypass_login_backoff = False

    async def _async_update_data(self):
        """Update data via library. Called by update_coordinator periodically."""

        try:
            await self.async_run(self.client.refresh)
        except Exception as err:
            if is_auth_error(err):
                self.auth_failed = True
                raise ConfigEntryAuthFailed(f"Credentials rejected: {err}") from err

            self.consecutive_failures += 1

            # Without a poll that worked there is nothing cached to fall back
            # to. A vehicle can be in the list already: the client adds it
            # before its status is fetched, and setting up with that half of
            # the data would leave out most entities until a reload.
            if not self.client.vehicles or self.last_successful_update is None:
                if self._may_start_without_status(err):
                    return True
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
            # An empty vehicle list answered at setup would set the entry up
            # without any entity, the lock included, until a reload
            if self.last_successful_update is None and not self.client.vehicles:
                raise UpdateFailed("The account lists no vehicle yet")

            self.consecutive_failures = 0
            self.last_successful_update = dt_util.utcnow()
            self._fire_placeholder_events()
            self._save_vehicle_stores()

            # Only after a successful poll: each of these calls can take until
            # the request timeout while the cloud is down, and they hold the
            # client lock, so commands would queue behind them for minutes
            await self._async_update_extra_data()

        return True

    def _may_start_without_status(self, err: Exception) -> bool:
        """Whether setup goes on although its first status poll failed.

        A status outage while Home Assistant starts would keep the entry
        retrying without entities, the lock included, and an automation
        locking the car at night would be skipped. Once the vehicles are
        listed and their entities registered by an earlier start, they come
        back with their states unknown, and the commands can be used.
        """
        if self.config_entry.state is not ConfigEntryState.SETUP_IN_PROGRESS:
            return False
        if not self.client.vehicles:
            return False
        if not er.async_entries_for_config_entry(
            er.async_get(self.hass), self.config_entry.entry_id
        ):
            return False
        _LOGGER.warning(
            "Vehicle status not available while setting up, starting with the "
            "states unknown: %s",
            err,
        )
        return True

    def _fire_placeholder_events(self) -> None:
        """Tell automations about each placeholder status left out, once.

        The cloud keeps returning the same one until the vehicle uploads
        again, so it is told once per upload, and once more after a restart.
        """
        # A copy: a command running in the executor may poll meanwhile
        for vin, placeholder in list(self.client.placeholders.items()):
            if self._placeholders_told.get(vin) == placeholder:
                continue
            self._placeholders_told[vin] = dict(placeholder)
            uploaded = placeholder.get("uploaded")
            self.hass.bus.async_fire(
                EVENT_PLACEHOLDER_DISCARDED,
                {
                    "vin": vin,
                    "uploaded": uploaded.isoformat() if uploaded else None,
                    "range": placeholder.get("range"),
                },
            )

    async def _async_update_extra_data(self) -> None:
        """Fetch extra data (VHR, maintenance, schedules, SVLA) for all vehicles.

        The health report and the maintenance history change at most daily,
        so they are fetched on a slow interval instead of on every poll. That
        halves the requests per poll and lowers the risk of rate limiting.
        These are reads of what the cloud holds, none of them wakes a vehicle.
        """

        now = dt_util.utcnow()

        for vin in list(self.client.vehicles):
            vehicle = self.client.vehicles.get(vin)
            if vehicle is None:
                continue

            # Timed from the attempt, so a vehicle whose report keeps failing
            # is not asked again on every poll
            last = self._last_slow_attempt.get(vin)
            if last is None or now - last >= SLOW_EXTRA_DATA_INTERVAL:
                self._last_slow_attempt[vin] = now
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

            # Only electrified vehicles have charge schedules. EMEA vehicles
            # report theirs inline, and the v4 schedule endpoint only answers
            # HTTP 500 for them.
            if getattr(vehicle, "state_of_charge", None) is not None and not (
                supports_emea_schedules(self.client, vehicle)
            ):
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

    async def async_set_charge_schedule(self, vin: str, schedule: dict) -> None:
        """Set a charge schedule"""

        await self._async_vehicle_action(
            "Set charge schedule",
            vin,
            "SCHEDULE_V4",
            self.client.set_charge_schedule_verify,
            vin,
            schedule,
        )

    async def async_set_emea_charge_schedule(
        self, vin: str, slot: int, **changes: Any
    ) -> None:
        """Change one inline charge schedule slot of an EMEA vehicle.

        The backend replaces the whole list on every write. The list is read
        fresh and written within one hold of the client lock, so two changes
        made one right after the other cannot undo each other, and changes
        made in the app since the last poll are kept. Nothing is sent, and the
        vehicle not woken, when the slot already has the requested values.
        Days are given as a list of weekday names.
        """

        self._check_can_send(vin)
        what = "Set charge schedule"

        async with self._client_lock:
            try:
                schedules = await self._async_run_locked(
                    self.client.plan_charge_schedule_emea,
                    vin,
                    slot,
                    changes,
                    on_request=True,
                )
            except ValueError as err:
                raise HomeAssistantError(f"{what} failed: {err}") from err
            except Exception as err:
                if is_auth_error(err):
                    self.auth_failed = True
                raise UconnectRequestError(f"{what} failed: {err}") from err

            if schedules is None:
                return

            if (record := await self._async_record_manual(vin, SCHEDULE_COMMAND)) is None:
                return
            try:
                result = await self._async_run_locked(
                    self.client.send_charge_schedules_emea,
                    vin,
                    schedules,
                    True,
                    on_request=True,
                )
            except PinRejectedError as err:
                async_get_wake_guard(self.hass).forget(vin, record)
                await self._async_save_pin_rejection()
                raise PinRejected(self.client.api.pin_locked) from err
            except Exception as err:
                # As for any other command: rejected credentials stop further
                # attempts, and a write refused outright never woke the car
                if is_auth_error(err):
                    self.auth_failed = True
                if _refused_before_queued(err):
                    async_get_wake_guard(self.hass).forget(vin, record)
                raise UconnectRequestError(f"{what} failed: {err}") from err

        await self._async_finish_action(what, record, result)

    async def async_command(
        self,
        vin: str,
        cmd: Command,
        *,
        automatic: bool = False,
        code: str | None = None,
    ) -> None:
        """Execute the given command.

        automatic is set by the deep refresh while charging, which the wake
        guard has already allowed and recorded. code is what came with an
        unlock, see check_unlock_code.
        """

        if cmd.name in UNLOCK_COMMANDS:
            self.check_unlock_code(code)

        await self._async_vehicle_action(
            f"Command {cmd.name}",
            vin,
            cmd.name,
            self.client.command_verify,
            vin,
            cmd,
            automatic=automatic,
        )

    async def async_set_charging_level(self, vin: str, level: str) -> None:
        """Set the charging level"""

        if level not in CHARGING_LEVELS_BY_NAME:
            raise ServiceValidationError(f"Invalid charging level: {level}")

        await self._async_vehicle_action(
            "Set charging level",
            vin,
            "CHARGE_PREFERENCE",
            self.client.set_charging_level_verify,
            vin,
            CHARGING_LEVELS_BY_NAME[level],
        )

    def _check_can_send(self, vin: str) -> None:
        """Raise if no command can be sent at all, before anything else."""

        if self.auth_failed:
            raise HomeAssistantError(
                "The Uconnect login was rejected. Reauthenticate the integration "
                "before sending commands."
            )
        if not self.client.api.pin:
            self._raise_pin_missing()
        if self.client.api.pin_rejected:
            raise PinRejected(self.client.api.pin_locked)
        if vin not in self.client.vehicles:
            raise ServiceValidationError(f"Vehicle {vin} is currently not available")

    @staticmethod
    def _raise_pin_missing() -> NoReturn:
        raise ServiceValidationError(
            "Commands need the Uconnect PIN, set it in the integration options.",
            translation_domain=DOMAIN,
            translation_key="pin_missing",
        )

    async def _async_record_manual(self, vin: str, command: str):
        """Check a command sent on request against the guard and record it.

        Returns None, and nothing is to be sent, when the vehicle confirmed the
        same command moments ago: the vehicle is in that state already, and
        reporting an error would stop the automation that sent it. The record
        is on disk before the command goes out.
        """

        guard = async_get_wake_guard(self.hass)
        try:
            guard.check_manual_command(vin, command)
        except AlreadyDone as err:
            _LOGGER.info("%s not sent again: %s", command, err)
            return None
        except Blocked as err:
            raise ServiceValidationError(
                f"{command} not sent: {err}",
                translation_domain=DOMAIN,
                translation_key=err.key,
                translation_placeholders={
                    "command": command,
                    "minutes": str(
                        max(int(err.wait.total_seconds() // 60) + 1, 1)
                        if err.wait
                        else 1
                    ),
                },
            ) from err

        record = guard.record_command(vin, command)
        await guard.async_flush()
        return record

    async def _async_vehicle_action(
        self,
        what: str,
        vin: str,
        command: str,
        func: Callable[..., Any],
        *args: Any,
        automatic: bool = False,
    ) -> None:
        """Run a vehicle action, refresh afterwards and report the outcome.

        Errors from the library are turned into UconnectRequestError so the
        UI shows the reason instead of "Unknown error". A falsy result means
        the vehicle refused, which is reported only after the refresh so the
        entities show what the vehicle really did.
        """

        self._check_can_send(vin)
        record = None
        if not automatic:
            if (record := await self._async_record_manual(vin, command)) is None:
                return

        try:
            result = await self.async_run(func, *args, on_request=not automatic)
        except HomeAssistantError:
            raise
        except PinRejectedError as err:
            # Only the PIN went out, the vehicle was not reached
            if record is not None:
                async_get_wake_guard(self.hass).forget(vin, record)
            await self._async_save_pin_rejection()
            raise PinRejected(self.client.api.pin_locked) from err
        except Exception as err:
            if is_auth_error(err):
                self.auth_failed = True
            # Refused by the cloud before it was queued, e.g. a deep refresh
            # of a 500e that is not plugged in, or stopped at the PIN
            # authentication: it never reached the vehicle, so it holds back
            # no later command
            refused = _refused_before_queued(err)
            if record is not None and refused:
                async_get_wake_guard(self.hass).forget(vin, record)
            vehicle = self.client.get_vehicles().get(vin)
            # Only the command itself: a PIN authentication refused (rate
            # limit, service down) has nothing to do with the plug
            if (
                refused
                and not isinstance(err, PinAuthError)
                and command in DEEP_REFRESH_COMMANDS
                and getattr(vehicle, "plugged_in", None) is False
            ):
                raise RefreshNeedsPlug from err
            raise UconnectRequestError(f"{what} failed: {err}") from err

        await self._async_finish_action(what, record, result)

    async def async_restore_confirmed_states(self) -> None:
        """Take back the changes confirmed before the last restart."""
        self.client.restore_confirmed_states(await self._confirmed_states.async_load())

    async def async_restore_last_charges(self) -> None:
        """Take back the last real state of charge from before the restart."""
        self.client.restore_last_charges(await self._last_charge.async_load())

    async def async_restore_pin_rejection(self) -> None:
        """Take back a rejection of the PIN set now from before the restart."""
        entry_id = self.config_entry.entry_id
        stored = (await self._pin_rejection.async_load()).get(entry_id)
        if stored is None:
            return
        if isinstance(stored, dict):
            self.client.api.restore_pin_rejection(
                stored.get("pin"), stored.get("locked"), entry_id
            )
        # One for a PIN since replaced, e.g. at a reauthentication, would come
        # back if that PIN were set again
        if not self.client.api.pin_rejected:
            await self._async_save_pin_rejection()

    async def _async_save_pin_rejection(self) -> None:
        api = self.client.api
        entry_id = self.config_entry.entry_id
        entries = (
            {entry_id: {"pin": api.pin_rejection_fingerprint(entry_id), "locked": api.pin_locked}}
            if api.pin_rejected
            else {}
        )
        try:
            await self._pin_rejection.async_load()
            if self._pin_rejection.update([entry_id], entries):
                await self._pin_rejection.async_save()
        except Exception as err:
            _LOGGER.debug("Unable to save the PIN rejection: %s", err)

    async def async_clear_pin_rejection(self) -> None:
        """Let a rejected PIN be sent again, e.g. once unlocked in the app."""
        async with self._client_lock:
            self.client.api.clear_pin_rejection()
        await self._async_save_pin_rejection()

    def _save_vehicle_stores(self) -> None:
        """Keep the stores in step with the client after a poll.

        A confirmed state the vehicle has since replaced is dropped here too,
        or a restart within its lifetime would bring it back while the status
        call still fails.
        """
        vins = list(self.client.vehicles)
        if self._confirmed_states.update(vins, self.client.confirmed_states()):
            self._confirmed_states.async_delay_save(CONFIRMED_STATES_SAVE_DELAY)
        if self._last_charge.update(vins, self.client.last_charges()):
            self._last_charge.async_delay_save(LAST_CHARGE_SAVE_DELAY)

    async def _async_save_confirmed_states(self) -> None:
        try:
            if self._confirmed_states.update(
                list(self.client.vehicles), self.client.confirmed_states()
            ):
                await self._confirmed_states.async_save()
        except Exception as err:
            _LOGGER.debug("Unable to save the confirmed states: %s", err)

    async def _async_finish_action(self, what: str, record, result: Any) -> None:
        if result and record is not None:
            async_get_wake_guard(self.hass).command_confirmed(record)
        if result:
            await self._async_save_confirmed_states()

        await self.async_refresh()

        if not result:
            raise HomeAssistantError(f"{what} was not confirmed by the vehicle")

    async def update_options(self, hass: HomeAssistant, config_entry: ConfigEntry):
        self.update_interval = timedelta(
            minutes=config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        )
        self.charging_refresh_interval = self._read_charging_refresh_interval(
            config_entry
        )
        new_pin = self._read_pin(config_entry)
        pin_changed = new_pin != self.client.api.pin
        # Under the lock, so a PIN authentication already running finishes
        # with the old PIN before its token is dropped
        async with self._client_lock:
            self.client.set_pin(new_pin)
        if pin_changed:
            await self._async_save_pin_rejection()

        # A dormant deep refresh schedule has nothing left to re-arm it, so a
        # charge already under way has to be told the options allow one now,
        # or that they no longer do
        for sensor in self.extrapolated_soc_sensors.values():
            sensor.resume_deep_refresh(pin_changed=pin_changed)


async def async_retry_pin(hass: HomeAssistant, entry: ConfigEntry, pin: str) -> None:
    """Let a rejected PIN be sent again, whether the entry is loaded or not.

    Saving the options with the same PIN is how the user says it is right
    again: it may have been unlocked in the official app, and Home Assistant
    does not tell the entry about options saved unchanged. A new PIN takes
    the place of the rejected one once the options apply; releasing the old
    one before that would let it go out once more.
    """
    coordinator = getattr(entry, "runtime_data", None)
    if isinstance(coordinator, UconnectDataUpdateCoordinator):
        if pin == coordinator.client.api.pin:
            await coordinator.async_clear_pin_rejection()
        return

    if pin != UconnectDataUpdateCoordinator._read_pin(entry):
        return
    brand = BRANDS_BY_NAME.get(BRANDS.get(entry.data.get(CONF_BRAND_REGION)))
    if brand is not None:
        forget_pin_rejection(brand.login_url, entry.data.get(CONF_USERNAME))
    store = VehicleStore.shared(hass, PIN_REJECTION_STORAGE_KEY)
    await store.async_load()
    if store.update([entry.entry_id], {}):
        await store.async_save()
