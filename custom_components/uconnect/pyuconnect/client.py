from dataclasses import dataclass, field, fields, replace
from dataclasses_json import dataclass_json
from typing import Any, Dict
from datetime import datetime, timedelta, timezone
from time import monotonic, sleep
import logging

from requests.exceptions import HTTPError, RequestException

from .api import (
    API,
    AuthenticationError,
    ChargingLevel,
    DEFAULT_TIMEOUT,
    LoginError,
    PinAuthError,
    PinRejectedError,
    _masked_vin,
)
from .brands import Brand
from .command import (
    COMMAND_CHARGE,
    COMMAND_CHARGE_V4,
    COMMAND_DEEP_REFRESH,
    COMMAND_DEEP_REFRESH_V2,
    Command,
    COMMANDS_BY_NAME,
)

_LOGGER = logging.getLogger(__name__)


def convert(v: Any) -> Any:
    if not isinstance(v, str):
        return v

    if v == "null":
        return None

    try:
        v = int(v)
    except (ValueError, TypeError):
        try:
            v = float(v)
        except (ValueError, TypeError):
            pass

    return v


def sg(dct: Any, *keys: str) -> Any:
    if not isinstance(dct, dict):
        return None

    for key in keys:
        # A block sent as null is missing, it must not fail the whole refresh
        if not isinstance(dct, dict):
            return None
        try:
            dct = dct[key]
        except KeyError:
            return None

    return convert(dct)


def sg_num(dct: Any, *keys: str) -> int | float | None:
    """Gets a nested number, None for anything else such as "NA".

    A placeholder kept as text would make a numeric sensor fail to write its
    state, and the battery estimate fail to compare it.
    """

    v = sg(dct, *keys)
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v
    return None


def sg_eq(dct: Any, expect: Any, *keys: str) -> bool | None:
    v = sg(dct, *keys)

    if v is None:
        return None

    return v == expect


def sg_eq_str(dct: Any, expect: str, *keys: str) -> bool:
    v = sg(dct, *keys)

    if not isinstance(v, str):
        return False

    return v.lower() == expect.lower()


def sg_str(dct: Any, *keys: str) -> str | None:
    """Gets a nested value as the string the API sent, without conversion.

    sg converts numeric looking strings, which turns a nickname like "500"
    into a number.
    """

    if not isinstance(dct, dict):
        return None

    for key in keys:
        if not isinstance(dct, dict) or key not in dct:
            return None
        dct = dct[key]

    if not isinstance(dct, str) or dct in ("", "null"):
        return None

    return dct


def _bool(value: Any) -> bool | None:
    """Reads a flag sent as a JSON boolean or, now and then, as a string.

    bool() would read the string "false" as true, which for a schedule slot
    being written back means switching on what was off.
    """

    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("true", "1", "yes", "on"):
            return True
        if text in ("false", "0", "no", "off"):
            return False
        return None
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    return None


def sg_bool(dct: Any, *keys: str) -> bool | None:
    """Gets a nested flag, None when it is missing or not a flag"""

    for key in keys:
        if not isinstance(dct, dict) or key not in dct:
            return None
        dct = dct[key]

    return _bool(dct)


def _flag(d: dict, key: str, default: bool) -> bool:
    value = _bool(d.get(key))
    return default if value is None else value


def sg_state(dct: Any, expect: str, *keys: str) -> bool | None:
    """Compares a nested state string, treating a missing or NA state as unknown.

    The remote status reports parts a vehicle does not have, like the rear
    doors of a three door car, as NA. Comparing that to LOCKED would report
    them as unlocked.
    """

    v = sg_str(dct, *keys)
    if v is None or v.upper() == "NA":
        return None

    return v.upper() == expect.upper()


CHARGING_LEVELS = {
    "DEFAULT": 0,
    "LEVEL_1": 1,
    "LEVEL_2": 2,
    "LEVEL_3": 3,
    "LEVEL_4": 4,
    "LEVEL_5": 5,
    # EMEA firmware reports the preference as LEVEL_ONE..LEVEL_FIVE
    "LEVEL_ONE": 1,
    "LEVEL_TWO": 2,
    "LEVEL_THREE": 3,
    "LEVEL_FOUR": 4,
    "LEVEL_FIVE": 5,
}


@dataclass_json
@dataclass
class Location:
    longitude: float | None = None
    latitude: float | None = None
    altitude: float | None = None
    bearing: float | None = None
    is_approximate: bool | None = None
    updated: datetime | None = None

    def __repr__(self):
        return f"lat: {self.latitude}, lon: {self.longitude} (updated {self.updated})"


@dataclass_json
@dataclass
class ScheduledDays:
    monday: bool = False
    tuesday: bool = False
    wednesday: bool = False
    thursday: bool = False
    friday: bool = False
    saturday: bool = False
    sunday: bool = False


@dataclass_json
@dataclass
class ChargeSchedule:
    """A single EV charge schedule slot.

    EMEA vehicles (e.g. Fiat New 500e) expose up to three of these inline in
    the v3 vehicle status under ``evInfo.schedules``. The North-American
    ``/v4/.../ev/schedule/`` endpoint is not used by these cars and returns
    HTTP 500. Writing is done via ``Client.set_charge_schedule_emea`` which
    POSTs the full list back to ``/v2/.../ev/schedule/``.
    """

    enable: bool = False
    start_time: str = "00:00"
    end_time: str = "00:00"
    days: ScheduledDays = field(default_factory=ScheduledDays)
    charge_to_full: bool = False
    cabin_priority: bool = False
    repeat_schedule: bool = True
    schedule_type: str = "CHARGE"

    @staticmethod
    def from_api(d: dict) -> "ChargeSchedule":
        days = d.get("scheduledDays")
        if not isinstance(days, dict):
            days = {}
        return ChargeSchedule(
            enable=_flag(d, "enableScheduleType", False),
            start_time=d.get("startTime", "00:00"),
            end_time=d.get("endTime", "00:00"),
            days=ScheduledDays(
                monday=_flag(days, "monday", False),
                tuesday=_flag(days, "tuesday", False),
                wednesday=_flag(days, "wednesday", False),
                thursday=_flag(days, "thursday", False),
                friday=_flag(days, "friday", False),
                saturday=_flag(days, "saturday", False),
                sunday=_flag(days, "sunday", False),
            ),
            charge_to_full=_flag(d, "chargeToFull", False),
            cabin_priority=_flag(d, "cabinPriority", False),
            repeat_schedule=_flag(d, "repeatSchedule", True),
            schedule_type=d.get("scheduleType", "CHARGE"),
        )

    def to_api(self) -> dict:
        """Serialise back into the exact shape the backend expects."""
        return {
            "chargeToFull": self.charge_to_full,
            "scheduleType": self.schedule_type,
            "enableScheduleType": self.enable,
            "scheduledDays": {
                "sunday": self.days.sunday,
                "saturday": self.days.saturday,
                "tuesday": self.days.tuesday,
                "wednesday": self.days.wednesday,
                "thursday": self.days.thursday,
                "friday": self.days.friday,
                "monday": self.days.monday,
            },
            "startTime": self.start_time,
            "endTime": self.end_time,
            "cabinPriority": self.cabin_priority,
            "repeatSchedule": self.repeat_schedule,
        }


@dataclass_json
@dataclass
class Vehicle:
    vin: str

    # General info
    nickname: str
    make: str
    model: str
    year: str
    region: str

    sdp: str | None = None
    image_url: str | None = None
    fuel_type: str | None = None

    # Status
    ignition_on: bool | None = None
    trunk_locked: bool | None = None
    liftgate_locked: bool | None = None
    hvac_on: bool | None = None

    odometer: float | None = None
    odometer_unit: str | None = None
    days_to_service: int | None = None
    distance_to_service: int | None = None
    distance_to_service_unit: str | None = None
    distance_to_empty: int | None = None
    distance_to_empty_unit: str | None = None
    range_gas: int | None = None
    range_gas_unit: str | None = None
    range_total: int | None = None
    range_total_unit: str | None = None
    battery_voltage: float | None = None
    battery_state_of_charge: str | None = None
    oil_level: int | None = None
    fuel_low: bool | None = None
    fuel_amount: int | None = None

    # EV related
    plugged_in: bool | None = None
    ev_running: bool | None = None
    charging: bool | None = None
    charging_level: int | None = None
    charging_level_preference: int | None = None
    # The upload whose LEVEL_ONE was not taken, see _update_vehicle
    charging_level_skipped: Any = field(default=None, repr=False, compare=False)
    state_of_charge: float | None = None
    time_to_fully_charge_l3: int | None = None
    time_to_fully_charge_l2: int | None = None
    time_to_fully_charge_l1: int | None = None
    ev_head_seat: bool | None = None
    ev_cabin_cond: bool | None = None
    # EV charge schedules (EMEA vehicles expose these inline in v3 status
    # under evInfo.schedules; the /v4/.../ev/schedule/ endpoint returns 500
    # for these cars). Read-only view; write via set_charge_schedule_emea.
    charge_schedules: list[ChargeSchedule] = field(default_factory=list)

    # Wheels
    wheel_front_left_pressure: float | None = None
    wheel_front_left_pressure_unit: str | None = None
    wheel_front_left_pressure_warning: bool | None = None
    wheel_front_right_pressure: float | None = None
    wheel_front_right_pressure_unit: str | None = None
    wheel_front_right_pressure_warning: bool | None = None
    wheel_rear_left_pressure: float | None = None
    wheel_rear_left_pressure_unit: str | None = None
    wheel_rear_left_pressure_warning: bool | None = None
    wheel_rear_right_pressure: float | None = None
    wheel_rear_right_pressure_unit: str | None = None
    wheel_rear_right_pressure_warning: bool | None = None

    # Doors
    door_driver_locked: bool | None = None
    door_passenger_locked: bool | None = None
    door_rear_left_locked: bool | None = None
    door_rear_right_locked: bool | None = None

    # Windows
    window_driver_closed: bool | None = None
    window_passenger_closed: bool | None = None

    location: Location | None = None
    supported_commands: list[str] = field(default_factory=list)
    enabled_services: list[str] = field(default_factory=list)

    # Trip computers A and B and the traction battery health, as far as the
    # vehicle reports them in the v3 status
    trip_a_distance: float | None = None
    trip_a_distance_unit: str | None = None
    trip_b_distance: float | None = None
    trip_b_distance_unit: str | None = None
    battery_soh: float | None = None

    timestamp_info: datetime | None = None
    timestamp_status: datetime | None = None
    # When the vehicle itself last uploaded each block, which is what tells
    # how fresh the values are; timestamp_info is when the cloud answered
    timestamp_vehicle_info: datetime | None = None
    timestamp_ev_info: datetime | None = None

    def __repr__(self):
        return f"{self.vin} (nick: {self.nickname})"


# The times to full, in the order _update_vehicle keeps them with the state
# of charge
_CHARGE_TIMES = (
    "time_to_fully_charge_l1",
    "time_to_fully_charge_l2",
    "time_to_fully_charge_l3",
)

# A saved charge stands in for a placeholder uploaded at most this long after
# it. Kept over weeks with upstream, it would be shown as current for days.
SAVED_CHARGE_MAX_AGE = 24 * 3600


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _placeholder_range(p: Any) -> int | float | None:
    """The electric range of a status that is a placeholder, else None.

    Now and then the cloud sends a state of charge of 0 next to an electric
    range that says otherwise, the times to full 0 as well (a 500e: 0 % and
    171 km, uploaded as it was switched off). A battery that is really empty
    has no range left, so the 0 is a placeholder. Only these values are: the
    lock states uploaded with it came with their own time and were right.
    """

    batt = sg(p, "evInfo", "battery")
    electric_range = sg_num(batt, "distanceToEmpty", "value")
    if (
        sg_num(batt, "stateOfCharge") == 0
        and electric_range is not None
        and electric_range > 0
    ):
        return electric_range
    return None


def _update_vehicle(
    v: Vehicle, p: dict, saved_charge: tuple[Any, Any, Any, Any] | None = None
) -> Vehicle:
    vi = sg(p, "vehicleInfo")
    ev = sg(p, "evInfo")
    batt = sg(ev, "battery")

    v.battery_voltage = sg_num(vi, "batteryInfo", "batteryVoltage", "value")
    v.battery_state_of_charge = sg_num(vi, "batteryInfo", "batteryStateOfCharge")
    # An unreadable state is unknown, not a definite "not charging"
    v.charging = sg_state(batt, "CHARGING", "chargingStatus")
    v.charging_level = CHARGING_LEVELS.get(sg_str(batt, "chargingLevel"), None)
    # chargePowerPreference is reported as a word-form string (EMEA:
    # LEVEL_ONE..LEVEL_FIVE; NA: LEVEL_1..LEVEL_5). Map it to the same 1..5
    # integer scale as charging_level for consistency (README shows int 5).
    _pref_raw = sg_str(ev, "chargePowerPreference")
    _uploaded = sg(ev, "timestamp")
    if (
        CHARGING_LEVELS.get(_pref_raw) == 1
        and v.charging_level_preference not in (None, 1)
        and sg_state(ev, "OFF", "ignitionStatus")
        and sg_bool(batt, "plugInStatus") is False
        and sg_num(batt, "timeToFullyChargeL3") == 0
        and v.charging_level_skipped in (None, _uploaded)
    ):
        # Minutes after a switch-off a 500e not plugged in now and then
        # uploads LEVEL_ONE, nobody having changed it, and the real one again
        # with its next upload. Shown, a level set by an automation would be
        # skipped as set already, or sent again and wake the vehicle. Only
        # this one upload is skipped: sent again, the level was really set.
        v.charging_level_skipped = _uploaded
    else:
        v.charging_level_skipped = None
        if _pref_raw in (None, "NOT_USED_VALUE"):
            v.charging_level_preference = None
        else:
            v.charging_level_preference = CHARGING_LEVELS.get(_pref_raw, None)

    v.plugged_in = sg_bool(batt, "plugInStatus")
    last_charge = (
        v.state_of_charge,
        v.time_to_fully_charge_l1,
        v.time_to_fully_charge_l2,
        v.time_to_fully_charge_l3,
    )
    v.state_of_charge = sg_num(batt, "stateOfCharge")

    v.days_to_service = sg_num(vi, "daysToService")
    v.distance_to_service = sg_num(vi, "distanceToService", "distanceToService", "value")
    v.distance_to_service_unit = sg(
        vi, "distanceToService", "distanceToService", "unit"
    )
    # A range of 0 is a value, it must not fall through to the fuel range
    v.distance_to_empty = _first(
        sg_num(batt, "distanceToEmpty", "value"),
        sg_num(vi, "fuel", "distanceToEmpty", "value"),
    )
    v.distance_to_empty_unit = _first(
        sg(batt, "distanceToEmpty", "unit"), sg(vi, "fuel", "distanceToEmpty", "unit")
    )
    v.range_gas = sg_num(batt, "gasRange")
    v.range_gas_unit = v.distance_to_empty_unit
    v.range_total = sg_num(batt, "totalRange")
    v.range_total_unit = v.distance_to_empty_unit

    v.fuel_low = sg_bool(vi, "fuel", "isFuelLevelLow")
    v.fuel_amount = sg_num(vi, "fuel", "fuelAmountLevel")
    v.oil_level = sg_num(vi, "oilLevel", "oilLevel")

    v.ignition_on = sg_state(ev, "ON", "ignitionStatus")

    csai = sg(ev, "chargeScheduleAdditionalInfo")
    v.ev_head_seat = sg_bool(csai, "headSeat")
    v.ev_cabin_cond = sg_bool(csai, "cabinCond")

    # EMEA charge schedules live inline in the v3 status. Newer NA firmware
    # instead exposes evInfo.chargeSchedules (different layout) and the v4
    # endpoint; we only parse the inline EMEA "schedules" list here.
    sched_raw = sg(ev, "schedules")
    if isinstance(sched_raw, list):
        if all(isinstance(x, dict) for x in sched_raw):
            v.charge_schedules = [ChargeSchedule.from_api(x) for x in sched_raw]
        else:
            _LOGGER.debug("Ignoring charge schedules that are not all objects")
            v.charge_schedules = []

    v.time_to_fully_charge_l3 = sg_num(batt, "timeToFullyChargeL3")
    v.time_to_fully_charge_l2 = sg_num(batt, "timeToFullyChargeL2")
    v.time_to_fully_charge_l1 = sg_num(batt, "timeToFullyChargeL1")

    # Some vehicles report -1
    if v.time_to_fully_charge_l3 is not None and v.time_to_fully_charge_l3 < 0:
        v.time_to_fully_charge_l3 = None
    if v.time_to_fully_charge_l2 is not None and v.time_to_fully_charge_l2 < 0:
        v.time_to_fully_charge_l2 = None
    if v.time_to_fully_charge_l1 is not None and v.time_to_fully_charge_l1 < 0:
        v.time_to_fully_charge_l1 = None

    # The state of charge of a placeholder status is not taken. The last real
    # reading stands in, which the client keeps also over a restart (see
    # Client.last_charges); without one the last values stay, else unknown
    if (electric_range := _placeholder_range(p)) is not None:
        _LOGGER.debug(
            "Ignoring a state of charge of 0 next to a range of %s", electric_range
        )
        if saved_charge is not None:
            last_charge = saved_charge
        (
            v.state_of_charge,
            v.time_to_fully_charge_l1,
            v.time_to_fully_charge_l2,
            v.time_to_fully_charge_l3,
        ) = last_charge

    odometer = sg_num(vi, "odometer", "odometer", "value")
    # A reading without a unit is in the last one, as the sensor shows it
    odometer_unit = sg_str(vi, "odometer", "odometer", "unit") or v.odometer_unit
    # An odometer never goes back. Home Assistant reads a lower value of a
    # total increasing sensor as a meter reset, so a single bad reading would
    # add the whole distance to the long-term statistics once it recovers.
    # A reading left out keeps the last one, so a 0 after it is still caught.
    # A 0 is no reading at all: right after a restart there is no last one to
    # compare it with, and it would read as a reset when the real one follows.
    if not isinstance(odometer, (int, float)) or isinstance(odometer, bool) or odometer == 0:
        pass
    elif (
        isinstance(v.odometer, (int, float))
        and str(odometer_unit or "").strip().lower()
        == str(v.odometer_unit or "").strip().lower()
        and odometer < v.odometer
    ):
        _LOGGER.debug(
            "Ignoring odometer reading %s below the last one %s", odometer, v.odometer
        )
    else:
        v.odometer = odometer
        v.odometer_unit = odometer_unit

    tyres = vi.get("tyrePressure") if isinstance(vi, dict) else None
    if isinstance(tyres, list):
        tp = {
            x["type"]: x
            for x in tyres
            if isinstance(x, dict) and isinstance(x.get("type"), str)
        }

        v.wheel_front_left_pressure = sg_num(tp, "FL", "pressure", "value")
        v.wheel_front_left_pressure_unit = sg(tp, "FL", "pressure", "unit")
        v.wheel_front_left_pressure_warning = sg_bool(tp, "FL", "warning")

        v.wheel_front_right_pressure = sg_num(tp, "FR", "pressure", "value")
        v.wheel_front_right_pressure_unit = sg(tp, "FR", "pressure", "unit")
        v.wheel_front_right_pressure_warning = sg_bool(tp, "FR", "warning")

        v.wheel_rear_left_pressure = sg_num(tp, "RL", "pressure", "value")
        v.wheel_rear_left_pressure_unit = sg(tp, "RL", "pressure", "unit")
        v.wheel_rear_left_pressure_warning = sg_bool(tp, "RL", "warning")

        v.wheel_rear_right_pressure = sg_num(tp, "RR", "pressure", "value")
        v.wheel_rear_right_pressure_unit = sg(tp, "RR", "pressure", "unit")
        v.wheel_rear_right_pressure_warning = sg_bool(tp, "RR", "warning")

    trips = sg(vi, "tripsInfo", "trips")
    trips = (
        {
            t["name"]: t
            for t in trips
            if isinstance(t, dict) and isinstance(t.get("name"), str)
        }
        if isinstance(trips, list)
        else {}
    )
    v.trip_a_distance = sg_num(trips, "TripA", "totalDistance", "value")
    v.trip_a_distance_unit = sg(trips, "TripA", "totalDistance", "unit")
    v.trip_b_distance = sg_num(trips, "TripB", "totalDistance", "value")
    v.trip_b_distance_unit = sg(trips, "TripB", "totalDistance", "unit")

    v.battery_soh = sg_num(vi, "hVBatSOH")

    v.timestamp_info = _ms_timestamp(sg(p, "timestamp"))
    v.timestamp_vehicle_info = _ms_timestamp(sg(vi, "timestamp"))
    v.timestamp_ev_info = _ms_timestamp(sg(ev, "timestamp"))

    return v


def _first(*values: Any) -> Any:
    """Returns the first value that is not None"""

    return next((x for x in values if x is not None), None)


def _ms_timestamp(value: Any) -> datetime | None:
    """Converts a millisecond epoch timestamp, ignoring anything else.

    One out of range value must not fail the whole refresh, and a value in
    seconds is read as such rather than as a moment in 1970.
    """

    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        return None

    seconds = value if value < 1e11 else value / 1000
    try:
        return datetime.fromtimestamp(seconds).astimezone()
    except (OverflowError, OSError, ValueError):
        return None


# Blocks of the status payload that are carried over from the last good one
# when a response leaves them out, so a partial answer does not clear them
_STATUS_BLOCKS = ("vehicleInfo", "evInfo")

# A block is carried over for at most this long. A vehicle that stopped
# reporting it for good must not show its last values forever.
STATUS_BLOCK_MAX_AGE = 24 * 3600


def _has_block(payload: Any, block: str) -> bool:
    value = payload.get(block) if isinstance(payload, dict) else None
    return isinstance(value, dict) and bool(value)


def _without_older_blocks(previous: dict | None, current: dict) -> dict:
    """Leaves out the blocks of current uploaded before those of previous.

    An older upload, as a lagging cloud may serve, would bring back the values
    the newer one replaced: a parked vehicle as switched on and charged
    higher. _merge_status carries the newer one over instead.
    """

    kept = dict(current)
    for block in _STATUS_BLOCKS:
        uploaded = sg_num(current, block, "timestamp")
        last = sg_num(previous, block, "timestamp")
        if uploaded is not None and last is not None and uploaded < last:
            _LOGGER.debug("Ignoring %s uploaded before the last one", block)
            del kept[block]
    return kept


def _merge_status(
    previous: dict | None,
    current: dict,
    fresh_at: dict[str, float] | None = None,
    now: float | None = None,
) -> dict:
    """Fills status blocks missing from current with those of previous.

    fresh_at holds when each block last came with a response and is updated
    here; a block older than STATUS_BLOCK_MAX_AGE is no longer carried over.
    """

    fresh_at = fresh_at if fresh_at is not None else {}
    now = now if now is not None else monotonic()

    merged = dict(current)
    for block in _STATUS_BLOCKS:
        if _has_block(current, block):
            fresh_at[block] = now
        elif (
            previous
            and _has_block(previous, block)
            and now - fresh_at.get(block, now) < STATUS_BLOCK_MAX_AGE
        ):
            merged[block] = previous[block]

    return merged


TERMINAL_SUCCESS = ("success", "succeeded", "completed", "complete")
# The notifications spell a failure "Fail"
TERMINAL_FAILURE = (
    "failure",
    "failed",
    "fail",
    "error",
    "rejected",
    "cancelled",
    "canceled",
    "timeout",
    "expired",
)
# Failures after which the vehicle may still have carried the command out.
# How a failed unlock is spelled is not known, only the event notifications
# were seen to say "Fail", so an unlock reported so leaves the doors unknown.
TERMINAL_UNKNOWN = ("fail", "cancelled", "canceled", "timeout", "expired")

# States the remote status stopped reporting are kept for at most this long.
# Shown as locked for days while the status call fails, a lock would be
# trusted. Grouped by the key the remote status reports them under.
REMOTE_STATE_MAX_AGE = 24 * 3600
REMOTE_STATE_GROUPS = {
    "doors": (
        "door_driver_locked",
        "door_passenger_locked",
        "door_rear_left_locked",
        "door_rear_right_locked",
    ),
    "windows": ("window_driver_closed", "window_passenger_closed"),
    "trunk": ("trunk_locked",),
    "liftGate": ("liftgate_locked",),
    "evRunning": ("ev_running",),
    "hvac": ("hvac_on",),
}

# A change the vehicle confirmed is shown, and the next one planned from it,
# until the cloud reports it or anything newer. The status the cloud returns
# can lag behind, and a second change planned from the old list would undo
# the first.
WRITE_OVERRIDE_MAX_AGE = 3600

# Lock commands and the states they set. The vehicle often uploads no new
# status after a remote lock, so the cloud keeps reporting the state from
# before for hours. The state confirmed is shown until the vehicle uploads a
# newer one, which it does when it is unlocked by key, for a day at most. The
# report from before is no truer after that day: the states are unknown until
# a newer one.
LOCK_OVERRIDE_MAX_AGE = 24 * 3600
# Kept with the confirmed states, see Client._drop_states_reported_while_on
REPORTED_BEFORE_OFF = "reported_before_switch_off"
REPORTED_AT_OFF = "reported_at_switch_off"
# Kept with them too: a restart between the last poll that saw the vehicle on
# and the first that sees it off would take the report made at the switch-off
# for the parked car's
SWITCHED_ON_AT = "switched_on_at"
SWITCHED_OFF_AT = "switched_off_at"
# A report this close to the switch-off was made with the driver still in
# the seat
SWITCH_OFF_REPORT_WINDOW = 60
# The groups whose locked or closed states such a report leaves out
SECURED_GROUPS = ("doors", "windows", "trunk", "liftGate")
LOCK_COMMAND_STATES: dict[str, tuple[tuple[str, ...], bool]] = {
    "RDL": (REMOTE_STATE_GROUPS["doors"], True),
    "RDU": (REMOTE_STATE_GROUPS["doors"], False),
    "ROTRUNKLOCK": (("trunk_locked",), True),
    "ROTRUNKUNLOCK": (("trunk_locked",), False),
    "ROLIFTGATELOCK": (("liftgate_locked",), True),
    "ROLIFTGATEUNLOCK": (("liftgate_locked",), False),
}

# How long the final status of a command is polled for
COMMAND_STATUS_TIMEOUT = timedelta(seconds=60)


def _refused_before_sent(err: BaseException) -> bool:
    """Whether a command failed before anything reached the vehicle: the login
    or the PIN authentication failed, whatever the answer, or the cloud refused
    it outright with a 4xx answer."""

    if isinstance(
        err, (AuthenticationError, LoginError, PinAuthError, PinRejectedError)
    ):
        return True
    status = getattr(getattr(err, "response", None), "status_code", None)
    return isinstance(status, int) and 400 <= status < 500


class Client:
    def __init__(
        self,
        email: str,
        password: str,
        pin: str,
        brand: Brand,
        disable_tls_verification: bool = False,
        dev_mode: bool = False,
        trace: bool = False,
        timeout: float | tuple[float, float] = DEFAULT_TIMEOUT,
    ):
        self.api = API(
            email,
            password,
            pin,
            brand,
            disable_tls_verification=disable_tls_verification,
            dev_mode=dev_mode,
            trace=trace,
            timeout=timeout,
        )
        self.vehicles: Dict[str, Vehicle] = {}
        self._last_status: Dict[str, dict] = {}
        # The last placeholder status of each vehicle, see _placeholder_range:
        # when the vehicle uploaded it and the range it gave
        self.placeholders: Dict[str, Dict[str, Any]] = {}
        # The last state of charge each vehicle really reported, see
        # last_charges
        self._last_charge: Dict[str, Dict[str, Any]] = {}
        self._block_fresh_at: Dict[str, Dict[str, float]] = {}
        # When the remote status last reported each group of states
        self._remote_fresh_at: Dict[str, Dict[str, float]] = {}
        # The parts each vehicle reported in its last remote status
        self._remote_parts: Dict[str, set[str]] = {}
        # The time of each remote status the vehicle was switched off after,
        # see _drop_states_reported_while_on
        self._reported_before_off: Dict[str, datetime] = {}
        # The time of each remote status made at the switch-off
        self._reported_at_off: Dict[str, datetime] = {}
        # When each vehicle was last seen on, and first seen off after that
        self._switched_on_at: Dict[str, datetime] = {}
        self._switched_off_at: Dict[str, datetime] = {}
        # Whether the last location read of each vehicle gave a position
        self._location_read: Dict[str, bool] = {}
        # Confirmed changes the cloud may not show yet, see
        # WRITE_OVERRIDE_MAX_AGE: value, when it was written and monotonic
        self._written: Dict[str, Dict[str, tuple[Any, datetime, float]]] = {}

    def set_debug(self, debug: bool):
        """Sets debug logging on/off"""

        self.api.set_debug(debug)

    def set_tls_verification(self, verify: bool):
        """Enable or disable TLS certificate verification"""

        self.api.set_tls_verification(verify)

    def set_pin(self, pin: str):
        self.api.set_pin(pin)

    def refresh(self):
        """Refreshes all the vehicle data and caches it locally"""

        vehicles = self.api.list_vehicles()

        listed = {x["vin"] for x in vehicles if isinstance(x, dict) and x.get("vin")}
        # A vehicle that left the account is dropped, its entities turn
        # unavailable. An empty list is taken as a hiccup rather than an
        # account without vehicles.
        if not listed and self.vehicles:
            # The vehicles are kept, but the poll fails: taken as a success,
            # no state would expire and a lock state shown as current forever
            raise ValueError("the vehicle list names none of the vehicles")
        if listed:
            for vin in [v for v in self.vehicles if v not in listed]:
                _LOGGER.info("Vehicle %s is no longer in the account", _masked_vin(vin))
                del self.vehicles[vin]
                self._last_status.pop(vin, None)
                self._block_fresh_at.pop(vin, None)
                self._remote_fresh_at.pop(vin, None)
                self._remote_parts.pop(vin, None)
                self._reported_before_off.pop(vin, None)
                self._reported_at_off.pop(vin, None)
                self._switched_on_at.pop(vin, None)
                self._switched_off_at.pop(vin, None)
                self._location_read.pop(vin, None)
                self._written.pop(vin, None)

        for x in vehicles:
            if not isinstance(x, dict) or not x.get("vin"):
                continue
            vin = x["vin"]

            if vin not in self.vehicles:
                # Names stay strings: a model like "1500" or "500" would
                # become a number, which the device registry refuses
                vehicle = Vehicle(
                    vin=vin,
                    nickname=sg_str(x, "nickname"),
                    make=sg_str(x, "make"),
                    model=sg_str(x, "modelDescription"),
                    year=sg(x, "tsoModelYear"),
                    region=sg_str(x, "soldRegion"),
                )
                self.vehicles[vin] = vehicle
            else:
                vehicle = self.vehicles[vin]

            vehicle.nickname = sg_str(x, "nickname")
            vehicle.sdp = sg(x, "sdp")
            vehicle.image_url = sg(x, "vehicleImageURL")
            vehicle.fuel_type = sg(x, "fuelType")

            # Taken from the list first, so a status call failing below does
            # not leave the vehicle without its commands. A list without any
            # keeps the last ones, as an empty status does: the actions
            # locking the car would be refused meanwhile.
            services = x.get("services")
            if isinstance(services, list) and services:
                enabled_services = [
                    v["service"]
                    for v in services
                    if isinstance(v, dict)
                    and isinstance(v.get("service"), str)
                    and _bool(v.get("vehicleCapable"))
                    and _bool(v.get("serviceEnabled"))
                ]
                vehicle.enabled_services = enabled_services
                supported = [v for v in enabled_services if v in COMMANDS_BY_NAME]
                # Vehicles listing only the newer variant of a command still
                # offer the command, which then goes out as that variant. The
                # action naming that variant is offered as well.
                for alias, command in (
                    ("DEEPREFRESH2", "DEEPREFRESH"),
                    ("CNOW2", "CNOW"),
                    ("CNOW2", COMMAND_CHARGE_V4.name),
                ):
                    if alias in enabled_services and command not in supported:
                        supported.append(command)
                vehicle.supported_commands = supported
            else:
                _LOGGER.debug("No services listed for %s, keeping the last ones", vin)

            try:
                info = self.api.get_vehicle(vin)
                previous = self._last_status.get(vin)
                fresh_at = self._block_fresh_at.setdefault(vin, {})

                # An empty answer comes back when both status endpoints fail,
                # which also happens now and then to vehicles that do support
                # them. Keep the last good values instead of clearing them all,
                # for STATUS_BLOCK_MAX_AGE at most as with a partial answer.
                if any(_has_block(info, b) for b in _STATUS_BLOCKS):
                    info = _without_older_blocks(previous, info)
                    if (placeholder := _placeholder_range(info)) is not None:
                        self.placeholders[vin] = {
                            "uploaded": _ms_timestamp(sg(info, "evInfo", "timestamp")),
                            "range": placeholder,
                        }
                    info = _merge_status(previous, info, fresh_at)
                elif previous:
                    kept = {k: v for k, v in previous.items() if k not in _STATUS_BLOCKS}
                    info = _merge_status(previous, kept, fresh_at)
                    if all(
                        _has_block(info, b) or not _has_block(previous, b)
                        for b in _STATUS_BLOCKS
                    ):
                        info = None
                else:
                    info = None

                if info is not None:
                    self._last_status[vin] = info
                    _update_vehicle(
                        vehicle,
                        info,
                        self._saved_charge(
                            vin, _ms_timestamp(sg(info, "evInfo", "timestamp"))
                        ),
                    )
                    if (
                        _placeholder_range(info) is None
                        and vehicle.state_of_charge is not None
                    ):
                        self._remember_charge(vin, vehicle)
                    self._apply_written(vin, vehicle, info)
                else:
                    _LOGGER.debug("No status data for %s, keeping the last values", vin)
            except HTTPError as err:
                status_code = (
                    err.response.status_code
                    if err.response is not None
                    else None
                )
                if status_code in (400, 404):
                    if vin in self.vehicles:
                        del self.vehicles[vin]

                    continue
                raise

            self._location_read[vin] = False
            try:
                loc = self.api.get_vehicle_location(vin)

                # A bad time must not throw away a good position
                updated = _ms_timestamp(sg(loc, "timeStamp"))

                # A position without coordinates, or at 0/0, would replace a
                # good one. So would one that is not a number, which the
                # tracker then fails to write.
                latitude, longitude = sg_num(loc, "latitude"), sg_num(loc, "longitude")
                if latitude is not None and longitude is not None and (
                    latitude,
                    longitude,
                ) != (0, 0):
                    vehicle.location = Location(
                        longitude=longitude,
                        latitude=latitude,
                        altitude=sg_num(loc, "altitude"),
                        bearing=sg_num(loc, "bearing"),
                        is_approximate=sg_bool(loc, "isLocationApprox"),
                        updated=updated,
                    )
                    self._location_read[vin] = True
            except Exception as err:
                _LOGGER.debug("Location not available for %s: %s", vin, err)

            try:
                s = self.api.get_vehicle_status(vin)

                if not isinstance(s, dict):
                    raise ValueError("remote status is not an object")

                # An older copy of the report, as a lagging cloud may serve,
                # would bring back states the newer one replaced
                uploaded = _ms_timestamp(sg(s, "timestamp"))
                if (
                    uploaded is not None
                    and vehicle.timestamp_status is not None
                    and uploaded < vehicle.timestamp_status
                ):
                    raise ValueError("remote status is older than the last one")

                if "doors" in s:
                    vehicle.door_driver_locked = sg_state(
                        s, "LOCKED", "doors", "driver", "status"
                    )
                    vehicle.door_passenger_locked = sg_state(
                        s, "LOCKED", "doors", "passenger", "status"
                    )
                    vehicle.door_rear_left_locked = sg_state(
                        s, "LOCKED", "doors", "leftRear", "status"
                    )
                    vehicle.door_rear_right_locked = sg_state(
                        s, "LOCKED", "doors", "rightRear", "status"
                    )

                if "windows" in s:
                    vehicle.window_driver_closed = sg_state(
                        s, "CLOSED", "windows", "driver", "status"
                    )
                    vehicle.window_passenger_closed = sg_state(
                        s, "CLOSED", "windows", "passenger", "status"
                    )

                # The ignition of an EV is read from its EV status, which an
                # engine reported as NA must not overwrite
                if (engine := sg_state(s, "ON", "engine", "status")) is not None:
                    vehicle.ignition_on = engine

                vehicle.trunk_locked = sg_state(s, "LOCKED", "trunk", "status")
                vehicle.liftgate_locked = sg_state(s, "LOCKED", "liftGate", "status")
                vehicle.ev_running = sg_state(s, "ON", "evRunning", "status")
                vehicle.hvac_on = sg_state(s, "ON", "hvac", "status")

                vehicle.timestamp_status = uploaded
                reported = {group for group in REMOTE_STATE_GROUPS if group in s}
                self._remote_parts[vin] = {
                    attr
                    for attrs in REMOTE_STATE_GROUPS.values()
                    for attr in attrs
                    if getattr(vehicle, attr) is not None
                }
            except Exception as err:
                _LOGGER.debug("Remote status not available for %s: %s", vin, err)
                reported = set()

            now = monotonic()
            fresh = self._remote_fresh_at.setdefault(vin, {})
            for group, attrs in REMOTE_STATE_GROUPS.items():
                if group in reported:
                    fresh[group] = now
                elif group in fresh and now - fresh[group] > REMOTE_STATE_MAX_AGE:
                    for attr in attrs:
                        setattr(vehicle, attr, None)

            self._note_switch_off(vin, vehicle)
            self._drop_states_reported_while_on(vin, vehicle)
            self._apply_written_locks(vin, vehicle, reported)

    def last_charges(self) -> dict[str, dict[str, Any]]:
        """The last state of charge each vehicle really reported, with its times
        to full, to keep over a restart. After one the cloud can answer with a
        placeholder first and repeat it until the vehicle uploads again, which
        can take days: the state of charge would stay unknown that long."""

        return {vin: dict(entry) for vin, entry in self._last_charge.items()}

    def restore_last_charges(self, data: Any) -> None:
        """Takes back what last_charges returned before a restart."""

        if not isinstance(data, dict):
            return
        for vin, entry in data.items():
            if not isinstance(entry, dict):
                continue
            soc = entry.get("state_of_charge")
            if not _is_number(soc) or not 0 <= soc <= 100:
                continue
            saved: Dict[str, Any] = {"state_of_charge": soc}
            for key in _CHARGE_TIMES:
                value = entry.get(key)
                saved[key] = value if _is_number(value) and value >= 0 else None
            uploaded = entry.get("uploaded")
            saved["uploaded"] = uploaded if isinstance(uploaded, str) else None
            self._last_charge[str(vin)] = saved

    def _remember_charge(self, vin: str, vehicle: "Vehicle") -> None:
        uploaded = getattr(vehicle, "timestamp_ev_info", None)
        self._last_charge[vin] = {
            "state_of_charge": vehicle.state_of_charge,
            **{key: getattr(vehicle, key, None) for key in _CHARGE_TIMES},
            "uploaded": uploaded.isoformat() if uploaded else None,
        }

    def _saved_charge(
        self, vin: str, uploaded: datetime | None = None
    ) -> tuple[Any, Any, Any, Any] | None:
        """The saved charge to show for a placeholder uploaded at uploaded,
        all unknown when it is older than SAVED_CHARGE_MAX_AGE before it."""

        entry = self._last_charge.get(vin)
        if entry is None:
            return None
        try:
            saved_at = datetime.fromisoformat(entry.get("uploaded"))
        except (TypeError, ValueError):
            saved_at = None
        if (
            uploaded is not None
            and saved_at is not None
            and saved_at.tzinfo is not None
            and (uploaded - saved_at).total_seconds() > SAVED_CHARGE_MAX_AGE
        ):
            return (None, None, None, None)
        return (entry["state_of_charge"], *(entry.get(key) for key in _CHARGE_TIMES))

    @staticmethod
    def _override_max_age(what: str) -> float:
        return LOCK_OVERRIDE_MAX_AGE if what.startswith("lock:") else WRITE_OVERRIDE_MAX_AGE

    def confirmed_states(self) -> dict[str, dict[str, Any]]:
        """The changes confirmed and not reported yet, to keep over a restart:
        lock states, charge schedules and the charging level. Also the remote
        status each vehicle was switched off after: a restart while the status
        upload fails would otherwise show the states of the drive again. And
        when each vehicle was last seen on and switched off, see
        _note_switch_off."""

        states = {
            vin: {
                what: {"value": value, "at": at.isoformat()}
                for what, (value, at, _) in entries.items()
            }
            for vin, entries in self._written.items()
            if entries
        }
        for key, markers in (
            (REPORTED_BEFORE_OFF, self._reported_before_off),
            (REPORTED_AT_OFF, self._reported_at_off),
            (SWITCHED_ON_AT, self._switched_on_at),
            (SWITCHED_OFF_AT, self._switched_off_at),
        ):
            for vin, reported in markers.items():
                states.setdefault(vin, {})[key] = {
                    "value": reported.isoformat(),
                    "at": reported.isoformat(),
                }
        return states

    def restore_confirmed_states(self, data: Any) -> None:
        """Takes back what confirmed_states returned before a restart."""

        if not isinstance(data, dict):
            return
        now = datetime.now(timezone.utc)
        for vin, entries in data.items():
            if not isinstance(entries, dict):
                continue
            for what, entry in entries.items():
                try:
                    value = entry["value"]
                    at = datetime.fromisoformat(entry["at"])
                except (KeyError, TypeError, ValueError):
                    continue
                what = str(what)
                if what in (REPORTED_BEFORE_OFF, REPORTED_AT_OFF):
                    # Applies only as long as the cloud has this report
                    if at.tzinfo is not None:
                        markers = (
                            self._reported_before_off
                            if what == REPORTED_BEFORE_OFF
                            else self._reported_at_off
                        )
                        markers[vin] = at
                    continue
                if what in (SWITCHED_ON_AT, SWITCHED_OFF_AT):
                    # Not one from a clock that was ahead: seen on in the
                    # future, no switch-off would be noticed until then
                    if at.tzinfo is not None and at <= now + timedelta(minutes=5):
                        times = (
                            self._switched_on_at
                            if what == SWITCHED_ON_AT
                            else self._switched_off_at
                        )
                        times.setdefault(vin, at)
                    continue
                if what.startswith("lock:"):
                    valid = isinstance(value, dict)
                elif what == "schedules":
                    valid = isinstance(value, list) and all(isinstance(x, dict) for x in value)
                elif what == "level":
                    valid = isinstance(value, int) and not isinstance(value, bool)
                else:
                    valid = False
                if not valid:
                    continue
                if at.tzinfo is None:
                    at = at.replace(tzinfo=timezone.utc)
                # Saved while the clock was ahead, its age would start anew
                # with every restart
                if at > now + timedelta(minutes=5):
                    continue
                age = max((now - at).total_seconds(), 0.0)
                if age > self._override_max_age(what):
                    if not (what.startswith("lock:") and value):
                        continue
                    # A day on, a lock is unknown until a newer report, as in
                    # _apply_written_locks, also when no poll saw the day pass
                    value = {a: None for a in value}
                self._written.setdefault(vin, {})[what] = (value, at, monotonic() - age)

    def _remember(
        self, vin: str, what: str, value: Any, at: datetime | None = None
    ) -> None:
        now = datetime.now(timezone.utc)
        at = at or now
        self._written.setdefault(vin, {})[what] = (
            value,
            at,
            monotonic() - max((now - at).total_seconds(), 0.0),
        )

    def _written_value(
        self,
        vin: str,
        what: str,
        reported: Any,
        uploaded: datetime | None,
        max_age: float = WRITE_OVERRIDE_MAX_AGE,
    ) -> Any:
        """The value written for what, as long as the cloud lags behind it.

        Dropped once the cloud reports it, once the vehicle uploaded anything
        after the write, which may be a change made in the app, or after an
        hour at most.
        """

        written = self._written.get(vin, {}).get(what)
        if written is None:
            return None

        value, at, since = written
        if (
            reported == value
            or (uploaded is not None and uploaded > at)
            or monotonic() - since > max_age
        ):
            del self._written[vin][what]
            return None
        return value

    def _apply_written(self, vin: str, vehicle: "Vehicle", info: dict) -> None:
        ev = info.get("evInfo") if isinstance(info, dict) else None
        uploaded = _ms_timestamp(sg(ev, "timestamp"))
        raw = ev.get("schedules") if isinstance(ev, dict) else None

        schedules = self._written_value(vin, "schedules", raw, uploaded)
        if schedules is not None:
            vehicle.charge_schedules = [ChargeSchedule.from_api(x) for x in schedules]

        # A level written here is taken from the upload confirming it, even
        # one _update_vehicle skipped as uploaded after a switch-off
        written = self._written.get(vin, {}).get("level")
        if written is not None and written[0] == CHARGING_LEVELS.get(
            sg_str(ev, "chargePowerPreference")
        ):
            vehicle.charging_level_preference = written[0]
            vehicle.charging_level_skipped = None
        level = self._written_value(
            vin, "level", vehicle.charging_level_preference, uploaded
        )
        if level is not None:
            vehicle.charging_level_preference = level

    def _remember_lock(
        self,
        vin: str,
        command: str,
        sent: datetime | None = None,
        confirmed: bool = True,
    ) -> None:
        """Shows a lock state the vehicle confirmed until it reports one.

        Any report made after sent replaces it: doors opened by key while the
        cloud confirmed the lock are reported with a time before the
        confirmation. Not confirmed, the states are unknown until such a report.
        """

        attrs, locked = LOCK_COMMAND_STATES[command]
        if not confirmed:
            locked = None
        vehicle = self.vehicles.get(vin)
        if vehicle is None:
            return
        # Parts the vehicle does not report stay unknown. Those it reported but
        # that are unknown since it was switched off are set as well.
        parts = self._remote_parts.get(vin, set())
        known = [
            a for a in attrs if a in parts or getattr(vehicle, a, None) is not None
        ] or list(attrs[:1])
        for attr in known:
            setattr(vehicle, attr, locked)
        self._remember(vin, f"lock:{','.join(attrs)}", {a: locked for a in known}, sent)

    def _note_switch_off(self, vin: str, vehicle: "Vehicle") -> None:
        """Keeps when the vehicle was last seen on, by its EV status or its
        remote status, and when it was first reported off after that."""

        info = self._last_status.get(vin)
        ev = info.get("evInfo") if isinstance(info, dict) else None
        uploaded = _ms_timestamp(sg(ev, "timestamp"))
        ignition = (sg_str(ev, "ignitionStatus") or "").upper()
        on_at = self._switched_on_at.get(vin)
        for seen in (
            uploaded if ignition == "ON" else None,
            vehicle.timestamp_status if vehicle.ev_running else None,
        ):
            if seen is not None and (on_at is None or seen > on_at):
                on_at = self._switched_on_at[vin] = seen
        off_at = self._switched_off_at.get(vin)
        if (
            ignition == "OFF"
            and uploaded is not None
            and on_at is not None
            and uploaded > on_at
            and (off_at is None or off_at < on_at)
        ):
            self._switched_off_at[vin] = uploaded
            # A lock confirmed while it was on is not known either, also when
            # the drive brought no new report
            for what, (_, at, _) in list(self._written.get(vin, {}).items()):
                if what.startswith("lock:") and at < uploaded:
                    del self._written[vin][what]

    def _drop_states_reported_while_on(self, vin: str, vehicle: "Vehicle") -> None:
        """Leaves out the remote states the vehicle reported while it was on,
        once it uploaded that it was switched off since.

        The doors lock while driving and can unlock on getting out. The vehicle
        mostly reports its states again when switched off, but not always: the
        doors locked during the drive were then shown as the state of the
        parked car, and nothing reminded of locking it. The states are unknown
        until the vehicle reports them again or confirms a lock command.

        A report made at the switch-off was taken before getting out as well.
        Of that one, only the states locked or closed are left out.
        """

        reported = vehicle.timestamp_status
        if reported is None:
            return
        at_off = self._reported_at_off.get(vin) == reported
        if self._reported_before_off.get(vin) != reported and not at_off:
            self._reported_before_off.pop(vin, None)
            self._reported_at_off.pop(vin, None)
            info = self._last_status.get(vin)
            ev = info.get("evInfo") if isinstance(info, dict) else None
            if vehicle.ev_running and not isinstance(ev, dict):
                # No EV status since a restart tells whether it was switched
                # off since. Left out for now, without keeping the report.
                for attrs in REMOTE_STATE_GROUPS.values():
                    for attr in attrs:
                        setattr(vehicle, attr, None)
                return
            switched_off = _ms_timestamp(sg(ev, "timestamp"))
            if (
                switched_off is None
                or (sg_str(ev, "ignitionStatus") or "").upper() != "OFF"
            ):
                return
            on_at = self._switched_on_at.get(vin)
            off_at = self._switched_off_at.get(vin)
            if switched_off > reported and (
                vehicle.ev_running or (on_at is not None and on_at > reported)
            ):
                # Kept until a newer report: an upload after switching on
                # again, or one without the ignition, would bring the states
                # back
                self._reported_before_off[vin] = reported
                # A lock confirmed while it was on is not known either
                for what, (_, at, _) in list(self._written.get(vin, {}).items()):
                    if what.startswith("lock:") and at < switched_off:
                        del self._written[vin][what]
            elif (
                on_at is not None
                and off_at is not None
                and on_at < reported
                and (reported - off_at).total_seconds() <= SWITCH_OFF_REPORT_WINDOW
            ):
                # Made after it was last seen on, and before or right after
                # the first upload seen off, which may be a later one parked
                self._reported_at_off[vin] = reported
                at_off = True
            else:
                return
        for group, attrs in REMOTE_STATE_GROUPS.items():
            for attr in attrs:
                if not at_off or (
                    group in SECURED_GROUPS and getattr(vehicle, attr) is True
                ):
                    setattr(vehicle, attr, None)

    def reported_parts(self, vin: str) -> set[str]:
        """The states the last remote status of a vehicle reported, also those
        that are unknown since, see _drop_states_reported_while_on."""

        return set(self._remote_parts.get(vin, ()))

    def switched_off_at(self, vin: str) -> datetime | None:
        """When the vehicle uploaded that it was switched off, if it was seen
        on before that, see _note_switch_off."""

        return self._switched_off_at.get(vin)

    def location_read(self, vin: str) -> bool:
        """Whether the last poll read a position of the vehicle. If not, the
        one shown may be older than what the cloud has."""

        return self._location_read.get(vin, False)

    def _apply_written_locks(
        self, vin: str, vehicle: "Vehicle", fetched: set[str]
    ) -> None:
        """Shows the confirmed locks, see _remember_lock.

        fetched are the groups the remote status reported in this poll.
        """

        uploaded = getattr(vehicle, "timestamp_status", None)
        for attrs, _ in {v[0]: v for v in LOCK_COMMAND_STATES.values()}.values():
            what = f"lock:{','.join(attrs)}"
            if what not in self._written.get(vin, {}):
                continue
            states = self._written[vin][what][0]
            # With a time, only a newer report replaces the lock. An older one
            # that happens to show it, or the states kept while the remote
            # status failed, would drop it, and the drive's states left out
            # later would leave the lock unknown although it was confirmed.
            # A report without a time replaces it once it shows the lock.
            group = next((g for g, a in REMOTE_STATE_GROUPS.items() if a == attrs), None)
            reported = (
                {a: getattr(vehicle, a, None) for a in states}
                if uploaded is None and group in fetched
                else None
            )
            _, at, since = self._written[vin][what]
            if (
                monotonic() - since > LOCK_OVERRIDE_MAX_AGE
                and (uploaded is None or uploaded <= at)
                and reported != states
            ):
                # A day on, the report from before the lock still is not the
                # state: an unlock shown locked again may leave the car open
                states = {a: None for a in states}
                self._written[vin][what] = (states, at, since)
                for attr in states:
                    setattr(vehicle, attr, None)
                continue
            written = self._written_value(
                vin, what, reported, uploaded, LOCK_OVERRIDE_MAX_AGE
            )
            if written is not None:
                for attr, value in written.items():
                    setattr(vehicle, attr, value)
            elif group not in fetched:
                # Not reported in this poll, the states are still the lock's
                for attr in states:
                    setattr(vehicle, attr, None)

    def get_vehicles(self):
        """Returns all vehicles data. Must execute refresh method before."""

        return self.vehicles

    def _get_commands_statuses(self, vin: str) -> dict:
        r = self.api.get_vehicle_notifications(vin)

        notifications = r.get("notifications") if isinstance(r, dict) else None
        items = notifications.get("items") if isinstance(notifications, dict) else None
        if not isinstance(items, list):
            return {}

        return {
            x["correlationId"]: (sg_str(x, "notification", "data", "status") or "").lower()
            for x in items
            if isinstance(x, dict) and "correlationId" in x
        }

    def _poll_correlation_id(
        self,
        vin: str,
        id: str,
        timeout: timedelta = COMMAND_STATUS_TIMEOUT,
        interval: timedelta = timedelta(seconds=2),
    ) -> bool:
        return self._poll_command_status(vin, id, timeout, interval) in TERMINAL_SUCCESS

    def _poll_command_status(
        self,
        vin: str,
        id: str,
        timeout: timedelta = COMMAND_STATUS_TIMEOUT,
        interval: timedelta = timedelta(seconds=2),
    ) -> str:
        """The final status of a command, one of TERMINAL_SUCCESS or
        TERMINAL_FAILURE."""

        # Monotonic, so a clock change during the poll neither stretches it,
        # holding the client for an hour, nor cuts it short
        deadline = monotonic() + timeout.total_seconds()
        last_error: Exception | None = None
        attempt = 0

        while monotonic() < deadline:
            # Most commands finish within seconds, slow ones take up to a
            # minute, so the wait grows to keep the request count down
            sleep(min(interval.seconds + attempt, 5))
            attempt += 1
            status_failed = False

            try:
                status = self.get_remote_operation_status(vin, id)

                result = sg(status, "status") or sg(status, "responseStatus")

                if isinstance(result, str):
                    result = result.lower()

                    if result in TERMINAL_SUCCESS + TERMINAL_FAILURE:
                        return result

            except LoginError as err:
                # The command went out: unlike before it, a failed login now
                # does not mean it never reached the vehicle
                raise Exception(f"unable to obtain execution status: {err}") from err
            except (ConnectionError, TimeoutError, ValueError, RequestException) as err:
                # Some brands/regions return 404 for the remote-operation status
                # endpoint even though the command itself succeeds (see issue #105).
                # Don't abort the poll on the HTTP error: fall through to the
                # notifications-based fallback below, which reports the status via
                # a working endpoint.
                last_error = err
                status_failed = True

            # The notifications are only needed when the status endpoint does
            # not work, but are checked now and then in case it never reports.
            # Not once a slow status call used up the time: each request can
            # take 40 s, while a lock waiting to go out waits for the client.
            if (status_failed or attempt % 3 == 0) and monotonic() < deadline:
                try:
                    r = self._get_commands_statuses(vin)
                except LoginError as err:
                    raise Exception(f"unable to obtain execution status: {err}") from err
                except (ConnectionError, TimeoutError, ValueError, RequestException) as err:
                    last_error = err
                    continue

                # A notification can also report the command as still running
                if r.get(id) in TERMINAL_SUCCESS + TERMINAL_FAILURE:
                    return r[id]

        raise Exception(
            f"unable to obtain execution status: timed out "
            f"(id {id}; last_error={last_error})"
        )

    def get_remote_operation_status(self, vin: str, correlation_id: str) -> dict:
        """Get the status of a remote operation by its correlation ID"""

        return self.api.get_remote_operation_status(vin, correlation_id)

    def command(self, vin: str, cmd: Command):
        """Execute a given command against a car with a given VIN"""

        return self.api.command(vin, self._resolve(vin, cmd))

    def _resolve(self, vin: str, cmd: Command) -> Command:
        """Picks the variant of a command the vehicle lists, as the apps do.

        Sent as the older variant first, such a command only goes out after
        that one was refused.
        """

        vehicle = self.vehicles.get(vin)
        services = getattr(vehicle, "enabled_services", None) or []
        if cmd == COMMAND_DEEP_REFRESH and "DEEPREFRESH2" in services:
            return COMMAND_DEEP_REFRESH_V2
        if cmd == COMMAND_CHARGE and "CNOW2" in services:
            return COMMAND_CHARGE_V4
        return cmd

    def command_verify(
        self,
        vin: str,
        cmd: Command,
    ) -> bool:
        """Execute a given command against a car with a given VIN and poll for the status"""

        sent = datetime.now(timezone.utc)
        # Shown locked while the vehicle may have unlocked would be the worst
        # error, so an unlock with an unknown outcome leaves the doors unknown
        unlock = cmd.name in LOCK_COMMAND_STATES and not LOCK_COMMAND_STATES[cmd.name][1]
        try:
            id = self.command(vin, cmd)
        except Exception as err:
            if unlock and not _refused_before_sent(err):
                # From now, as one still pending: the vehicle may have taken
                # it, and a report made while the request was open may still
                # show the doors locked
                self._remember_lock(vin, cmd.name, confirmed=False)
            raise
        try:
            status = self._poll_command_status(vin, id)
        except Exception:
            if unlock:
                # From now, as a confirmed unlock: a report made while it was
                # pending may still show the doors locked
                self._remember_lock(vin, cmd.name, confirmed=False)
            raise

        result = status in TERMINAL_SUCCESS
        if result and cmd.name in LOCK_COMMAND_STATES:
            # A lock from the time it was sent, see _remember_lock. An unlock
            # from its confirmation: a report made in between may still show
            # the doors locked.
            self._remember_lock(vin, cmd.name, None if unlock else sent)
        elif unlock and status in TERMINAL_UNKNOWN:
            self._remember_lock(vin, cmd.name, confirmed=False)
        return result

    def get_stolen_vehicle_status(self, vin: str) -> dict:
        """Get stolen vehicle locator (SVLA) status for a vehicle with a given VIN"""

        return self.api.get_stolen_vehicle_status(vin)

    def get_vehicle_subscription(self, vin: str) -> dict:
        """Get subscription status for a vehicle with a given VIN"""

        return self.api.get_vehicle_subscription(vin)

    def set_vehicle_nickname(self, vin: str, nickname: str) -> dict:
        """Set a nickname for a vehicle with a given VIN"""

        return self.api.set_vehicle_nickname(vin, nickname)

    def update_location(self, vin: str) -> str:
        """Trigger a fresh location update for a vehicle with a given VIN.

        Returns the correlation ID to poll for completion.
        """

        return self.api.update_location(vin)

    def get_eco_coaching_last_trip(self, vin: str) -> dict:
        """Get eco-coaching data for the last trip of a vehicle with a given VIN"""

        return self.api.get_eco_coaching_last_trip(vin)

    def get_eco_coaching_trips(self, vin: str) -> dict:
        """Get eco-coaching trip list for a vehicle with a given VIN"""

        return self.api.get_eco_coaching_trips(vin)

    def get_maintenance_history(self, vin: str) -> dict:
        """Get maintenance history for a vehicle with a given VIN"""

        return self.api.get_maintenance_history(vin)

    def get_vehicle_health_report(self, vin: str) -> dict:
        """Get vehicle health report for a vehicle with a given VIN"""

        return self.api.get_vehicle_health_report(vin)

    def get_vehicle_image(self, vin: str) -> dict:
        """Get vehicle image URL for a vehicle with a given VIN"""

        return self.api.get_vehicle_image(vin)

    def get_charge_schedules(self, vin: str) -> dict:
        """Get EV charge schedules for a vehicle with a given VIN"""

        return self.api.get_charge_schedules(vin)

    def set_charge_schedule(self, vin: str, schedule: dict):
        """Set an EV charge schedule on the vehicle with a given VIN"""

        return self.api.set_charge_schedule(vin, schedule)

    def set_charge_schedule_verify(self, vin: str, schedule: dict) -> bool:
        """Set an EV charge schedule and poll for the status"""

        id = self.api.set_charge_schedule(vin, schedule)
        return self._poll_correlation_id(vin, id)

    def set_charge_schedule_emea(
        self, vin: str, schedules: list[ChargeSchedule], verify: bool = False
    ):
        """Set EV charge schedules for EMEA vehicles.

        The backend replaces the *entire* schedule list on every write, so the
        full set of slots must always be sent — sending a single slot silently
        resets the others to their defaults. Callers should therefore take the
        vehicle's current ``charge_schedules``, modify the slot(s) they care
        about, and pass the whole list back in.

        Uses the ``CPPLUS`` command against ``/v2/.../ev/schedule/`` (confirmed
        working on Fiat New 500e / EMEA, where the NA ``/v4`` endpoint returns
        HTTP 500). Returns the correlation id, or the poll result if
        ``verify`` is True.
        """

        payload = [s.to_api() for s in schedules]
        id = self.api.set_charge_schedule_emea(vin, payload)
        if verify:
            return self._poll_correlation_id(vin, id)
        return id

    def plan_charge_schedule_emea(
        self, vin: str, slot: int, changes: dict[str, Any]
    ) -> list[dict] | None:
        """Work out the schedule list that changes one slot, None if unchanged.

        The current schedules are read from the vehicle status right before,
        not taken from the last poll: the backend replaces the whole list, so
        writing from older data would undo changes made in the app since. Keys
        the library does not know are kept as they are. This only reads, it
        does not wake the vehicle.

        changes may hold enable, start_time, end_time, charge_to_full,
        cabin_priority and days, a list of weekday names.
        """

        info = self.api.get_vehicle(vin)
        ev = info.get("evInfo") if isinstance(info, dict) else None
        raw = ev.get("schedules") if isinstance(ev, dict) else None
        if not isinstance(raw, list) or not all(isinstance(x, dict) for x in raw):
            raise ValueError(
                "the current charge schedules could not be read, nothing was changed"
            )
        # A change confirmed moments ago that the cloud does not show yet, or
        # one whose outcome is unknown: the vehicle may still apply it, and
        # planning from the older list would then write its values back
        uploaded = _ms_timestamp(sg(ev, "timestamp"))
        reported = raw
        written = self._written_value(vin, "schedules", raw, uploaded)
        confirmed = written is not None
        if not confirmed:
            written = self._written_value(vin, "schedules_unconfirmed", raw, uploaded)
        if written is not None and len(written) == len(raw):
            raw = written
            if confirmed:
                reported = written
        if not 0 <= slot < len(raw):
            raise ValueError(f"charge schedule {slot + 1} does not exist")

        current = ChargeSchedule.from_api(raw[slot])
        values = {
            k: changes[k]
            for k in ("enable", "start_time", "end_time", "charge_to_full", "cabin_priority")
            if k in changes
        }
        if "days" in changes:
            values["days"] = ScheduledDays(
                **{f.name: f.name in changes["days"] for f in fields(ScheduledDays)}
            )
        updated = replace(current, **values)

        # Enabled without a weekday it never starts, yet the write wakes the
        # vehicle. A slot that was never used has none.
        if (
            updated.enable
            and (not current.enable or "days" in changes)
            and not any(getattr(updated.days, f.name) for f in fields(ScheduledDays))
        ):
            raise ValueError(
                f"charge schedule {slot + 1} needs at least one weekday to be enabled"
            )

        # A slot that was never used has the type NONE, which the vehicle
        # ignores even when enabled
        if updated.enable and str(updated.schedule_type or "").upper() in ("", "NONE"):
            updated = replace(updated, schedule_type="CHARGE")

        # The same change as a write with an unknown outcome goes out again:
        # that one may never have reached the vehicle
        if updated == current and updated == ChargeSchedule.from_api(reported[slot]):
            _LOGGER.debug("Charge schedule %d of %s unchanged, not sent", slot + 1, vin)
            return None

        schedules = [dict(x) for x in raw]
        schedules[slot] = {**raw[slot], **updated.to_api()}
        return schedules

    def send_charge_schedules_emea(
        self, vin: str, schedules: list[dict], verify: bool = True
    ):
        """Send a complete EMEA schedule list, as planned above.

        Returns the poll result if verify is set, else the correlation id.
        """

        id = self.api.set_charge_schedule_emea(vin, schedules)
        try:
            result = self._poll_correlation_id(vin, id) if verify else id
        except Exception:
            # Sent, but whether the vehicle took it is unknown. Not shown, the
            # next change is only planned on top of it, see above.
            self._remember(vin, "schedules_unconfirmed", [dict(x) for x in schedules])
            raise

        # Shown right away, the next status the cloud returns may still be
        # the old one for a while
        if result:
            self._remember(vin, "schedules", [dict(x) for x in schedules])
            vehicle = self.vehicles.get(vin)
            if vehicle is not None:
                vehicle.charge_schedules = [
                    ChargeSchedule.from_api(x) for x in schedules
                ]

        return result

    def update_charge_schedule_emea(
        self, vin: str, slot: int, changes: dict[str, Any], verify: bool = True
    ):
        """Plan and send a change of one slot. True when there is nothing to send."""

        schedules = self.plan_charge_schedule_emea(vin, slot, changes)
        if schedules is None:
            return True
        return self.send_charge_schedules_emea(vin, schedules, verify)

    def set_charging_level(
        self, vin: str, level: ChargingLevel, max_soc: str | None = None
    ):
        """Set the charging level on the vehicle with a given VIN"""

        return self.api.set_charging_level(vin, level, max_soc=max_soc)

    def set_charging_level_verify(
        self, vin: str, level: ChargingLevel, max_soc: str | None = None
    ):
        """Set the charging level on the vehicle with a given VIN and poll for the status"""

        id = self.api.set_charging_level(vin, level, max_soc=max_soc)
        result = self._poll_correlation_id(vin, id)

        # Shown right away, the cloud reports the old level for a while
        preference = CHARGING_LEVELS.get(level.name)
        if result and preference is not None:
            self._remember(vin, "level", preference)
            if (vehicle := self.vehicles.get(vin)) is not None:
                vehicle.charging_level_preference = preference

        return result
