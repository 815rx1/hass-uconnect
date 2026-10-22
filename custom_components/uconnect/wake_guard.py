"""Bookkeeping of everything that wakes the vehicle.

Every remote command wakes the vehicle's telematics unit, which draws on the
12 V battery. The automatic deep refresh while charging therefore stays within
fixed limits, and those limits have to hold across everything that can happen
around it:

- restarts and crashes: the history is written to disk before each wake-up is
  sent, unlike entity restore data, which Home Assistant saves only every
  15 minutes and at a clean shutdown
- several config entries for the same vehicle (the same account added twice,
  or two accounts sharing a car): the history is kept per VIN for the whole
  Home Assistant instance, so they share one schedule
- the clock jumping, e.g. a Raspberry Pi without real-time clock booting with
  an old time: a wake-up dated in the future counts as just now, and one dated
  more than a day ahead is discarded so it cannot block refreshes for good

Commands sent on request are recorded too. They are never held back for
timing, only repeated and runaway ones are: a refresh right after another, or
more commands in an hour than a person sends, which is what an automation stuck
in a loop does. The same command again right after the vehicle confirmed it is
skipped, as it would change nothing. Locking and switching off are exempt from
all of this unless they repeat themselves, as a loop would.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import logging
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.storage import Store

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

STORAGE_KEY = f"{DOMAIN}.wake_history"
STORAGE_VERSION = 1
DATA_WAKE_GUARD = "_wake_guard"

# The automatic deep refresh never wakes the vehicle within this time after
# any command, whoever sent it
AUTO_REFRESH_MIN_SPACING = timedelta(minutes=15)
# and at most this many times within any 24 hours
AUTO_REFRESH_MAX_PER_DAY = 24
# A charge that has finished or stalled while the cloud still reports it as
# charging shows no gain in state of charge. After this many automatic
# refreshes in a row without new data, the session gets no more of them.
AUTO_REFRESH_MAX_WITHOUT_PROGRESS = 3
# A vehicle that does not answer holds the request open for a minute, so the
# session gives up after this many failures in a row
AUTO_REFRESH_MAX_FAILURES = 3
# No automatic refresh right after Home Assistant started. This bounds the
# wake-ups of a crash loop even if the history could not be read.
STARTUP_GRACE = timedelta(minutes=15)
# Charging that resumes within this time after it stopped continues the same
# session, so a wallbox switching on and off with solar surplus, or a charging
# flag flickering in the cloud data, does not start a new session each time
SESSION_MERGE_WINDOW = timedelta(minutes=30)

# Refresh commands sent on request only fetch data. An automation triggered by
# the data a refresh brings in would otherwise keep the vehicle awake.
REFRESH_COMMANDS = frozenset({"DEEPREFRESH", "DEEPREFRESH2", "VF"})
# The ones that bring in fresh charge data
DEEP_REFRESH_COMMANDS = frozenset({"DEEPREFRESH", "DEEPREFRESH2"})
MANUAL_REFRESH_COOLDOWN = timedelta(minutes=10)
MANUAL_REFRESH_MAX_PER_DAY = 48
# A deep refresh requested by hand this shortly before a charge was noticed
# is what made it show up, so it serves as the refresh at its start
MANUAL_START_REFRESH_WINDOW = timedelta(minutes=3)
# Commands that set a state, grouped by what they set. Sending one again right
# after the vehicle confirmed it does nothing but wake the vehicle once more,
# unless a command of the same group was sent in between.
COMMAND_GROUPS: dict[str, str] = {
    "RDL": "doors",
    "RDU": "doors",
    "ROTRUNKLOCK": "trunk",
    "ROTRUNKUNLOCK": "trunk",
    "ROLIFTGATELOCK": "liftgate",
    "ROLIFTGATEUNLOCK": "liftgate",
    "REON": "engine",
    "REOFF": "engine",
    "ROPRECOND": "climate",
    "ROPRECOND_OFF": "climate",
    "ROHVACON": "climate",
    "ROHVACOFF": "climate",
    "ROCOMFORTON": "climate",
    "ROCOMFORTOFF": "climate",
    "CNOW": "charge",
    "START_CHARGE": "charge",
}
STATE_COMMANDS = frozenset(COMMAND_GROUPS)
# Commands that leave the vehicle locked or switched off. They are never held
# back as a repeat, since the state shown may be out of date and a vehicle left
# unlocked or running is worse than one more wake-up, and the hourly limit only
# holds them back when they repeat themselves, as a loop would.
SECURING_COMMANDS = frozenset(
    {
        "RDL",
        "ROTRUNKLOCK",
        "ROLIFTGATELOCK",
        "REOFF",
        "ROPRECOND_OFF",
        "ROHVACOFF",
        "ROCOMFORTOFF",
    }
)
REPEAT_WINDOW = timedelta(seconds=60)
# More commands sent on request than this within an hour, or a day, is not a
# person but an automation stuck in a loop
MANUAL_MAX_PER_HOUR = 30
MANUAL_MAX_PER_DAY = 100

HISTORY_WINDOW = timedelta(hours=24)
# Timestamps further ahead than this can only come from a wrong clock
FUTURE_TOLERANCE = timedelta(days=1)
# Only bounds the file: the daily limits keep a day's history well below it
MAX_HISTORY = 500


class Decision(Enum):
    """Outcome of asking whether an automatic refresh may wake the vehicle."""

    ALLOWED = "allowed"
    WAIT = "wait"
    SESSION_DONE = "session_done"


@dataclass
class AutoRefreshDecision:
    decision: Decision
    wait: timedelta | None = None
    reason: str = ""
    # The wake-up recorded for an allowed refresh
    record: WakeRecord | None = None


class Blocked(Exception):
    """A command sent on request is held back, see the reason."""

    def __init__(self, reason: str, key: str, wait: timedelta | None = None) -> None:
        super().__init__(reason)
        self.key = key
        self.wait = wait


class AlreadyDone(Exception):
    """The vehicle confirmed this command moments ago, it need not go out."""


@dataclass
class WakeRecord:
    at: datetime
    command: str
    automatic: bool
    # Set once the vehicle confirmed the command
    confirmed: bool = False


@dataclass
class ChargeSession:
    """Automatic refresh bookkeeping of the current or last charging session."""

    started: datetime | None = None
    ended: datetime | None = None
    auto_refreshes: int = 0
    without_progress: int = 0
    failures: int = 0
    soc_at_last_refresh: float | None = None
    data_at_last_refresh: datetime | None = None

    @property
    def active(self) -> bool:
        return self.started is not None and self.ended is None


@dataclass
class VehicleWakes:
    wakes: list[WakeRecord] = field(default_factory=list)
    session: ChargeSession = field(default_factory=ChargeSession)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _age(now: datetime, then: datetime) -> timedelta:
    """Time since then, where a moment dated in the future counts as now."""
    return max(now - then, timedelta(0))


def _count(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _minutes(delta: timedelta) -> int:
    return max(int(delta.total_seconds() // 60) + 1, 1)


class WakeGuard:
    """Per-vehicle wake-up history and the limits that use it."""

    def __init__(self, hass: HomeAssistant) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY, atomic_writes=True
        )
        self._vehicles: dict[str, VehicleWakes] = {}
        # One-time changes already applied, kept with the history because
        # they also serve keeping the vehicle from being commanded by accident
        self._migrations: set[str] = set()
        self._started = _utcnow()

    async def async_load(self) -> None:
        """Load the history saved by earlier runs."""
        try:
            data = await self._store.async_load()
        except Exception as err:  # A damaged file must not stop the setup
            _LOGGER.warning("Unable to read the wake-up history: %s", err)
            data = None

        migrations = data.get("migrations") if isinstance(data, dict) else None
        if isinstance(migrations, list):
            self._migrations = {m for m in migrations if isinstance(m, str)}

        vehicles = data.get("vehicles") if isinstance(data, dict) else None
        if not isinstance(vehicles, dict):
            return

        for vin, raw in vehicles.items():
            if not isinstance(vin, str) or not isinstance(raw, dict):
                continue
            entry = VehicleWakes()
            for item in raw.get("wakes") or []:
                if not isinstance(item, dict) or (at := _parse(item.get("at"))) is None:
                    continue
                entry.wakes.append(
                    WakeRecord(
                        at=at,
                        command=str(item.get("command") or ""),
                        automatic=bool(item.get("automatic")),
                        confirmed=bool(item.get("confirmed")),
                    )
                )
            session = raw.get("session")
            if isinstance(session, dict):
                entry.session = ChargeSession(
                    started=_parse(session.get("started")),
                    ended=_parse(session.get("ended")),
                    auto_refreshes=_count(session.get("auto_refreshes")),
                    without_progress=_count(session.get("without_progress")),
                    failures=_count(session.get("failures")),
                    soc_at_last_refresh=_number(session.get("soc_at_last_refresh")),
                    data_at_last_refresh=_parse(session.get("data_at_last_refresh")),
                )
            self._vehicles[vin] = entry

    @callback
    def _data_to_save(self) -> dict[str, Any]:
        return {
            "migrations": sorted(self._migrations),
            "vehicles": {
                vin: {
                    "wakes": [
                        {
                            "at": _iso(w.at),
                            "command": w.command,
                            "automatic": w.automatic,
                            "confirmed": w.confirmed,
                        }
                        for w in entry.wakes
                    ],
                    "session": {
                        "started": _iso(entry.session.started),
                        "ended": _iso(entry.session.ended),
                        "auto_refreshes": entry.session.auto_refreshes,
                        "without_progress": entry.session.without_progress,
                        "failures": entry.session.failures,
                        "soc_at_last_refresh": entry.session.soc_at_last_refresh,
                        "data_at_last_refresh": _iso(entry.session.data_at_last_refresh),
                    },
                }
                for vin, entry in self._vehicles.items()
            }
        }

    @callback
    def _save(self) -> None:
        self._store.async_delay_save(self._data_to_save, 0)

    async def async_flush(self) -> None:
        """Write the history to disk now. Called before a wake-up is sent."""
        try:
            await self._store.async_save(self._data_to_save())
        except Exception as err:
            # The in-memory limits still apply, only a crash could lose this
            _LOGGER.warning("Unable to save the wake-up history: %s", err)

    @callback
    def _vehicle(self, vin: str, now: datetime) -> VehicleWakes:
        entry = self._vehicles.setdefault(vin, VehicleWakes())
        limit = now + FUTURE_TOLERANCE
        future = [w for w in entry.wakes if w.at > limit]
        if future:
            _LOGGER.warning(
                "Ignoring %d wake-ups of %s dated more than a day ahead, "
                "the clock was probably wrong",
                len(future),
                vin,
            )
        valid = [w for w in entry.wakes if w.at <= limit]
        kept = [w for w in valid if _age(now, w.at) < HISTORY_WINDOW]
        # The latest one is kept beyond the window, it still dates the last
        # wake-up
        if not kept and valid:
            kept = [max(valid, key=lambda w: w.at)]
        entry.wakes = sorted(kept, key=lambda w: w.at)[-MAX_HISTORY:]
        return entry

    @callback
    def is_migrated(self, key: str) -> bool:
        return key in self._migrations

    @callback
    def mark_migrated(self, key: str) -> None:
        self._migrations.add(key)
        self._save()

    # --- History ---------------------------------------------------------

    @callback
    def wakes(self, vin: str) -> list[WakeRecord]:
        """The recorded wake-ups of the last 24 hours, oldest first."""
        return list(self._vehicle(vin, _utcnow()).wakes)

    @callback
    def last_wake(self, vin: str, now: datetime | None = None) -> datetime | None:
        """When any command last woke the vehicle."""
        wakes = self._vehicle(vin, now or _utcnow()).wakes
        return wakes[-1].at if wakes else None

    @callback
    def last_auto_refresh(
        self, vin: str, now: datetime | None = None
    ) -> datetime | None:
        wakes = [w for w in self._vehicle(vin, now or _utcnow()).wakes if w.automatic]
        return wakes[-1].at if wakes else None

    @callback
    def auto_refreshes_last_day(self, vin: str, now: datetime | None = None) -> int:
        now = now or _utcnow()
        return sum(
            1
            for w in self._vehicle(vin, now).wakes
            if w.automatic and _age(now, w.at) < HISTORY_WINDOW
        )

    @callback
    def session(self, vin: str) -> ChargeSession:
        return self._vehicle(vin, _utcnow()).session

    # --- Commands sent on request ----------------------------------------

    @callback
    def check_manual_command(
        self, vin: str, command: str, now: datetime | None = None
    ) -> None:
        """Check whether a command sent on request should go out now.

        Raises Blocked if it is held back, and AlreadyDone if the vehicle
        confirmed the very same command moments ago, so sending it again would
        change nothing. A wake-up dated in the future only comes from a clock
        that was set back, and must not hold commands back for hours.
        """
        now = now or _utcnow()
        wakes = [w for w in self._vehicle(vin, now).wakes if w.at <= now]

        if command in REFRESH_COMMANDS:
            refreshes = [w for w in wakes if w.command in REFRESH_COMMANDS]
            if refreshes:
                since = _age(now, refreshes[-1].at)
                if since < MANUAL_REFRESH_COOLDOWN:
                    raise Blocked(
                        f"The vehicle was refreshed {int(since.total_seconds() // 60)} "
                        f"min ago. Try again in {_minutes(MANUAL_REFRESH_COOLDOWN - since)} min.",
                        "refresh_cooldown",
                        MANUAL_REFRESH_COOLDOWN - since,
                    )
            manual = [w for w in refreshes if not w.automatic]
            if len(manual) >= MANUAL_REFRESH_MAX_PER_DAY:
                wait = HISTORY_WINDOW - _age(now, manual[-MANUAL_REFRESH_MAX_PER_DAY].at)
                raise Blocked(
                    f"{MANUAL_REFRESH_MAX_PER_DAY} refreshes were requested within "
                    f"24 hours. Try again in {_minutes(wait)} min.",
                    "refresh_daily_limit",
                    wait,
                )

        # The last command that set the same thing, whatever its direction
        group = COMMAND_GROUPS.get(command)
        last_of_group = next(
            (
                w
                for w in reversed(wakes)
                if group is not None and COMMAND_GROUPS.get(w.command) == group
            ),
            None,
        )

        if (
            command not in SECURING_COMMANDS
            and last_of_group is not None
            and last_of_group.command == command
            and last_of_group.confirmed
            and now - last_of_group.at < REPEAT_WINDOW
        ):
            raise AlreadyDone(
                f"{command} was confirmed by the vehicle "
                f"{int((now - last_of_group.at).total_seconds())} s ago"
            )

        # Locking after unlocking, or switching off what was switched on,
        # always goes out
        if command in SECURING_COMMANDS and (
            last_of_group is None or last_of_group.command != command
        ):
            return

        last_hour = [
            w
            for w in wakes
            if not w.automatic and _age(now, w.at) < timedelta(hours=1)
        ]
        if len(last_hour) >= MANUAL_MAX_PER_HOUR:
            wait = timedelta(hours=1) - _age(now, last_hour[-MANUAL_MAX_PER_HOUR].at)
            raise Blocked(
                f"{MANUAL_MAX_PER_HOUR} commands were sent to the vehicle within an "
                f"hour, which looks like an automation stuck in a loop. Try again "
                f"in {_minutes(wait)} min.",
                "hourly_limit",
                wait,
            )

        last_day = [
            w for w in wakes if not w.automatic and _age(now, w.at) < HISTORY_WINDOW
        ]
        if len(last_day) >= MANUAL_MAX_PER_DAY:
            wait = HISTORY_WINDOW - _age(now, last_day[-MANUAL_MAX_PER_DAY].at)
            raise Blocked(
                f"{MANUAL_MAX_PER_DAY} commands were sent to the vehicle within 24 "
                f"hours, which looks like an automation stuck in a loop. Try again "
                f"in {_minutes(wait)} min.",
                "daily_limit",
                wait,
            )

    @callback
    def record_command(
        self, vin: str, command: str, now: datetime | None = None
    ) -> WakeRecord:
        """Record a command sent on request, right before it is sent."""
        now = now or _utcnow()
        record = WakeRecord(now, command, False)
        self._vehicle(vin, now).wakes.append(record)
        self._save()
        return record

    @callback
    def command_confirmed(self, record: WakeRecord) -> None:
        record.confirmed = True
        self._save()

    @callback
    def forget(self, vin: str, record: WakeRecord) -> None:
        """Take back a wake-up recorded for a command that never went out.

        An automatic refresh taken back no longer counts for its session, so
        the refresh at the start of a charge can still happen once whatever
        stopped it, such as a rejected PIN, is fixed.
        """
        entry = self._vehicle(vin, _utcnow())
        if not any(w is record for w in entry.wakes):
            return
        entry.wakes = [w for w in entry.wakes if w is not record]
        if record.automatic and entry.session.auto_refreshes:
            entry.session.auto_refreshes -= 1
        self._save()

    @callback
    def stop_session(self, vin: str) -> None:
        """No more automatic refreshes this charge, until reset_failures."""
        self._vehicle(vin, _utcnow()).session.failures = AUTO_REFRESH_MAX_FAILURES
        self._save()

    # --- Automatic refresh while charging --------------------------------

    @callback
    def begin_auto_refresh(
        self,
        vin: str,
        command: str,
        interval: timedelta | None,
        now: datetime | None = None,
    ) -> AutoRefreshDecision:
        """Decide on an automatic refresh and record it if it may go ahead.

        interval is the configured time between refreshes while charging, None
        when only the refresh at the start of a charge is wanted. A charging
        vehicle may be woken, so that first refresh always happens, within the
        limits below. The interval is enforced here, per vehicle, so that
        entries handling the same vehicle share one schedule.
        Checking and recording happen in one go in the event loop, so two
        callers cannot both get through.
        """
        now = now or _utcnow()
        entry = self._vehicle(vin, now)
        session = entry.session

        # A deep refresh asked for by hand that made this charge show up
        by_hand = [
            w
            for w in entry.wakes
            if not w.automatic
            and w.confirmed
            and w.command in DEEP_REFRESH_COMMANDS
            and session.started is not None
            and w.at >= session.started - MANUAL_START_REFRESH_WINDOW
        ]

        if interval is None and (session.auto_refreshes >= 1 or by_hand):
            return AutoRefreshDecision(
                Decision.SESSION_DONE,
                reason="only the refresh at the start of a charge is enabled",
            )

        if session.failures >= AUTO_REFRESH_MAX_FAILURES:
            return AutoRefreshDecision(
                Decision.SESSION_DONE,
                reason=f"{session.failures} refreshes in a row failed",
            )

        if session.without_progress >= AUTO_REFRESH_MAX_WITHOUT_PROGRESS:
            return AutoRefreshDecision(
                Decision.SESSION_DONE,
                reason=(
                    f"the last {session.without_progress} refreshes brought "
                    f"no new charge data"
                ),
            )

        if (since_start := now - self._started) < STARTUP_GRACE:
            return AutoRefreshDecision(
                Decision.WAIT,
                wait=STARTUP_GRACE - max(since_start, timedelta(0)),
                reason="Home Assistant started less than 15 minutes ago",
            )

        if entry.wakes:
            since = _age(now, entry.wakes[-1].at)
            if since < AUTO_REFRESH_MIN_SPACING:
                return AutoRefreshDecision(
                    Decision.WAIT,
                    wait=AUTO_REFRESH_MIN_SPACING - since,
                    reason="the vehicle was woken less than 15 minutes ago",
                )

        if interval is not None and session.started is not None:
            in_session = sorted(
                [w for w in entry.wakes if w.automatic and w.at >= session.started]
                + by_hand,
                key=lambda w: w.at,
            )
            if in_session and (since := _age(now, in_session[-1].at)) < interval:
                return AutoRefreshDecision(
                    Decision.WAIT,
                    wait=interval - since,
                    reason="the interval since the last refresh has not passed",
                )

        automatic = [
            w for w in entry.wakes if w.automatic and _age(now, w.at) < HISTORY_WINDOW
        ]
        if len(automatic) >= AUTO_REFRESH_MAX_PER_DAY:
            oldest = automatic[-AUTO_REFRESH_MAX_PER_DAY]
            return AutoRefreshDecision(
                Decision.WAIT,
                wait=max(HISTORY_WINDOW - _age(now, oldest.at), timedelta(minutes=1)),
                reason=f"{AUTO_REFRESH_MAX_PER_DAY} refreshes in the last 24 hours",
            )

        record = WakeRecord(now, command, True)
        entry.wakes.append(record)
        session.auto_refreshes += 1
        self._save()
        return AutoRefreshDecision(Decision.ALLOWED, record=record)

    @callback
    def auto_refresh_done(
        self,
        vin: str,
        state_of_charge: float | None,
        data_timestamp: datetime | None,
    ) -> None:
        """Track whether automatic refreshes still bring new charge data.

        A refresh counts as progress only if the state of charge rose. An
        unknown state of charge counts as none, and so does an upload time
        that did not move, since then the vehicle sent nothing new.
        """
        session = self._vehicle(vin, _utcnow()).session
        session.failures = 0

        progress = state_of_charge is not None and (
            session.soc_at_last_refresh is None
            or state_of_charge > session.soc_at_last_refresh
        )
        if (
            data_timestamp is not None
            and session.data_at_last_refresh is not None
            and data_timestamp <= session.data_at_last_refresh
        ):
            progress = False

        session.without_progress = 0 if progress else session.without_progress + 1
        if state_of_charge is not None:
            session.soc_at_last_refresh = state_of_charge
        if data_timestamp is not None:
            session.data_at_last_refresh = data_timestamp
        self._save()

    @callback
    def auto_refresh_failed(self, vin: str) -> int:
        session = self._vehicle(vin, _utcnow()).session
        session.failures += 1
        self._save()
        return session.failures

    @callback
    def reset_failures(self, vin: str) -> None:
        """Let the refreshes try again, e.g. after the PIN was changed."""
        session = self._vehicle(vin, _utcnow()).session
        if session.failures:
            session.failures = 0
            self._save()

    @callback
    def charging_started(self, vin: str, now: datetime | None = None) -> bool:
        """Note that the vehicle is charging, return whether a new session began.

        A session that is still running, or that ended less than
        SESSION_MERGE_WINDOW ago, carries on with its counters.
        """
        now = now or _utcnow()
        entry = self._vehicle(vin, now)
        session = entry.session
        if session.active:
            return False
        if session.ended is not None and _age(now, session.ended) < SESSION_MERGE_WINDOW:
            session.ended = None
            self._save()
            return False

        entry.session = ChargeSession(started=now)
        self._save()
        return True

    @callback
    def charging_stopped(self, vin: str, now: datetime | None = None) -> None:
        now = now or _utcnow()
        session = self._vehicle(vin, now).session
        if session.active:
            session.ended = now
            self._save()

    @callback
    def import_legacy(self, vin: str, times: list[datetime], charging: bool) -> None:
        """Take over the wake-ups earlier versions kept in the sensor state.

        Only when nothing is known about the vehicle yet. A charge that was
        under way continues as a session from the first of them, so the
        interval counts from the last one. The sensor closes the session if
        the vehicle turns out not to be charging any more.
        """
        now = _utcnow()
        entry = self._vehicle(vin, now)
        if entry.wakes or not times:
            return
        entry.wakes = [WakeRecord(t, "DEEPREFRESH", True) for t in times]
        if charging and entry.session.started is None:
            entry.session = ChargeSession(started=times[0], auto_refreshes=len(times))
        self._vehicle(vin, now)
        self._save()


async def async_setup_wake_guard(hass: HomeAssistant) -> WakeGuard:
    """Create and load the guard once for the Home Assistant instance."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    if (guard := domain_data.get(DATA_WAKE_GUARD)) is None:
        guard = WakeGuard(hass)
        await guard.async_load()
        domain_data[DATA_WAKE_GUARD] = guard
    return guard


@callback
def async_get_wake_guard(hass: HomeAssistant) -> WakeGuard:
    """Return the guard set up in async_setup."""
    return hass.data[DOMAIN][DATA_WAKE_GUARD]
