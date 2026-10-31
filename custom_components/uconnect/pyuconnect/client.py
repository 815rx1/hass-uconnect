from dataclasses import dataclass, field, fields, replace
from dataclasses_json import dataclass_json
from typing import Any, Dict
from datetime import datetime, timedelta, timezone
from time import monotonic, sleep
import logging

from requests.exceptions import HTTPError, RequestException

from .api import API, ChargingLevel, DEFAULT_TIMEOUT, _masked_vin
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
    v.charging = sg_eq(batt, "CHARGING", "chargingStatus")
    v.charging_level = CHARGING_LEVELS.get(sg(batt, "chargingLevel"), None)
    # chargePowerPreference is reported as a word-form string (EMEA:
    # LEVEL_ONE..LEVEL_FIVE; NA: LEVEL_1..LEVEL_5). Map it to the same 1..5
    # integer scale as charging_level for consistency (README shows int 5).
    _pref_raw = sg(ev, "chargePowerPreference")
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

    v.ignition_on = sg_eq(ev, "ON", "ignitionStatus")

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
    odometer_unit = sg(vi, "odometer", "odometer", "unit")
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
        and str(odometer_unit or "").lower() == str(v.odometer_unit or "").lower()
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
        tp = {x.get("type"): x for x in tyres if isinstance(x, dict)}

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
    trips = {t.get("name"): t for t in trips if isinstance(t, dict)} if isinstance(trips, list) else {}
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
TERMINAL_FAILURE = ("failure", "failed", "error", "rejected", "timeout")

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
# newer one, which it does when it is unlocked by key, for a day at most.
LOCK_OVERRIDE_MAX_AGE = 24 * 3600
# Kept with the confirmed states, see Client._drop_states_reported_while_on
REPORTED_BEFORE_OFF = "reported_before_switch_off"
LOCK_COMMAND_STATES: dict[str, tuple[tuple[str, ...], bool]] = {
    "RDL": (REMOTE_STATE_GROUPS["doors"], True),
    "RDU": (REMOTE_STATE_GROUPS["doors"], False),
    "ROTRUNKLOCK": (("trunk_locked",), True),
    "ROTRUNKUNLOCK": (("trunk_locked",), False),
    "ROLIFTGATELOCK": (("liftgate_locked",), True),
    "ROLIFTGATEUNLOCK": (("liftgate_locked",), False),
}


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
        if listed:
            for vin in [v for v in self.vehicles if v not in listed]:
                _LOGGER.info("Vehicle %s is no longer in the account", _masked_vin(vin))
                del self.vehicles[vin]
                self._last_status.pop(vin, None)
                self._block_fresh_at.pop(vin, None)
                self._remote_fresh_at.pop(vin, None)
                self._remote_parts.pop(vin, None)
                self._reported_before_off.pop(vin, None)
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
            # not leave the vehicle without its commands
            services = x.get("services")
            enabled_services = [
                v["service"]
                for v in (services if isinstance(services, list) else [])
                if isinstance(v, dict)
                and v.get("service")
                and sg(v, "vehicleCapable")
                and sg(v, "serviceEnabled")
            ]
            vehicle.enabled_services = enabled_services
            supported = [v for v in enabled_services if v in COMMANDS_BY_NAME]
            # Vehicles listing only the newer variant of a command still
            # offer the command, which then goes out as that variant
            for alias, command in (("DEEPREFRESH2", "DEEPREFRESH"), ("CNOW2", "CNOW")):
                if alias in enabled_services and command not in supported:
                    supported.append(command)
            vehicle.supported_commands = supported

            try:
                info = self.api.get_vehicle(vin)
                if (placeholder := _placeholder_range(info)) is not None:
                    self.placeholders[vin] = {
                        "uploaded": _ms_timestamp(sg(info, "evInfo", "timestamp")),
                        "range": placeholder,
                    }

                # An empty answer comes back when both status endpoints fail,
                # which also happens now and then to vehicles that do support
                # them. Keep the last good values instead of clearing them all.
                if any(_has_block(info, b) for b in _STATUS_BLOCKS):
                    info = _merge_status(
                        self._last_status.get(vin),
                        info,
                        self._block_fresh_at.setdefault(vin, {}),
                    )
                    self._last_status[vin] = info
                    _update_vehicle(vehicle, info, self._saved_charge(vin))
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

            try:
                loc = self.api.get_vehicle_location(vin)

                updated = (
                    datetime.fromtimestamp(loc["timeStamp"] / 1000).astimezone()
                    if "timeStamp" in loc
                    else None
                )

                # A position without coordinates, or at 0/0, would replace a
                # good one
                latitude, longitude = sg(loc, "latitude"), sg(loc, "longitude")
                if latitude is not None and longitude is not None and (
                    latitude,
                    longitude,
                ) != (0, 0):
                    vehicle.location = Location(
                        longitude=sg(loc, "longitude"),
                        latitude=sg(loc, "latitude"),
                        altitude=sg(loc, "altitude"),
                        bearing=sg(loc, "bearing"),
                        is_approximate=sg(loc, "isLocationApprox"),
                        updated=updated,
                    )
            except Exception as err:
                _LOGGER.debug("Location not available for %s: %s", vin, err)

            try:
                s = self.api.get_vehicle_status(vin)

                if not isinstance(s, dict):
                    raise ValueError("remote status is not an object")

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

                vehicle.timestamp_status = _ms_timestamp(sg(s, "timestamp"))
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

    def _saved_charge(self, vin: str) -> tuple[Any, Any, Any, Any] | None:
        entry = self._last_charge.get(vin)
        if entry is None:
            return None
        return (entry["state_of_charge"], *(entry.get(key) for key in _CHARGE_TIMES))

    @staticmethod
    def _override_max_age(what: str) -> float:
        return LOCK_OVERRIDE_MAX_AGE if what.startswith("lock:") else WRITE_OVERRIDE_MAX_AGE

    def confirmed_states(self) -> dict[str, dict[str, Any]]:
        """The changes confirmed and not reported yet, to keep over a restart:
        lock states, charge schedules and the charging level. Also the remote
        status each vehicle was switched off after: a restart while the status
        upload fails would otherwise show the states of the drive again."""

        states = {
            vin: {
                what: {"value": value, "at": at.isoformat()}
                for what, (value, at, _) in entries.items()
            }
            for vin, entries in self._written.items()
            if entries
        }
        for vin, reported in self._reported_before_off.items():
            states.setdefault(vin, {})[REPORTED_BEFORE_OFF] = {
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
                if what == REPORTED_BEFORE_OFF:
                    # Applies only as long as the cloud has this report
                    if at.tzinfo is not None:
                        self._reported_before_off[vin] = at
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
                age = max((now - at).total_seconds(), 0.0)
                if age > self._override_max_age(what):
                    continue
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

        level = self._written_value(
            vin, "level", vehicle.charging_level_preference, uploaded
        )
        if level is not None:
            vehicle.charging_level_preference = level

    def _remember_lock(
        self, vin: str, command: str, sent: datetime | None = None
    ) -> None:
        """Shows a lock state the vehicle confirmed until it reports one.

        Any report made after the command was sent replaces it: doors opened
        by key while the cloud confirmed the lock are reported with a time
        before the confirmation.
        """

        attrs, locked = LOCK_COMMAND_STATES[command]
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

    def _drop_states_reported_while_on(self, vin: str, vehicle: "Vehicle") -> None:
        """Leaves out the remote states the vehicle reported while it was on,
        once it uploaded that it was switched off since.

        The doors lock while driving and can unlock on getting out. The vehicle
        mostly reports its states again when switched off, but not always: the
        doors locked during the drive were then shown as the state of the
        parked car, and nothing reminded of locking it. The states are unknown
        until the vehicle reports them again or confirms a lock command.
        """

        reported = vehicle.timestamp_status
        if reported is None:
            return
        if self._reported_before_off.get(vin) != reported:
            self._reported_before_off.pop(vin, None)
            info = self._last_status.get(vin)
            ev = info.get("evInfo") if isinstance(info, dict) else None
            switched_off = _ms_timestamp(sg(ev, "timestamp"))
            if not (
                vehicle.ev_running
                and switched_off is not None
                and switched_off > reported
                and (sg_str(ev, "ignitionStatus") or "").upper() == "OFF"
            ):
                return
            # Kept until a newer report: an upload after switching on again,
            # or one without the ignition, would bring the states back
            self._reported_before_off[vin] = reported
        for attrs in REMOTE_STATE_GROUPS.values():
            for attr in attrs:
                setattr(vehicle, attr, None)

    def reported_parts(self, vin: str) -> set[str]:
        """The states the last remote status of a vehicle reported, also those
        that are unknown since, see _drop_states_reported_while_on."""

        return set(self._remote_parts.get(vin, ()))

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
            written = self._written_value(
                vin, what, reported, uploaded, LOCK_OVERRIDE_MAX_AGE
            )
            if written is not None:
                for attr, value in written.items():
                    setattr(vehicle, attr, value)

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
        timeout: timedelta = timedelta(seconds=60),
        interval: timedelta = timedelta(seconds=2),
    ) -> bool:
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

                    if result in TERMINAL_SUCCESS:
                        return True

                    if result in TERMINAL_FAILURE:
                        return False

            except (ConnectionError, TimeoutError, ValueError, RequestException) as err:
                # Some brands/regions return 404 for the remote-operation status
                # endpoint even though the command itself succeeds (see issue #105).
                # Don't abort the poll on the HTTP error: fall through to the
                # notifications-based fallback below, which reports the status via
                # a working endpoint.
                last_error = err
                status_failed = True

            # The notifications are only needed when the status endpoint does
            # not work, but are checked now and then in case it never reports
            if status_failed or attempt % 3 == 0:
                try:
                    r = self._get_commands_statuses(vin)
                except (ConnectionError, TimeoutError, ValueError, RequestException) as err:
                    last_error = err
                    continue

                # A notification can also report the command as still running
                if r.get(id) in TERMINAL_SUCCESS:
                    return True
                if r.get(id) in TERMINAL_FAILURE:
                    return False

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
        id = self.command(vin, cmd)
        result = self._poll_correlation_id(vin, id)
        if result and cmd.name in LOCK_COMMAND_STATES:
            self._remember_lock(vin, cmd.name, sent)
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
        # A change confirmed moments ago that the cloud does not show yet
        written = self._written_value(
            vin, "schedules", raw, _ms_timestamp(sg(ev, "timestamp"))
        )
        if written is not None and len(written) == len(raw):
            raw = written
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

        # A slot that was never used has the type NONE, which the vehicle
        # ignores even when enabled
        if updated.enable and str(updated.schedule_type or "").upper() in ("", "NONE"):
            updated = replace(updated, schedule_type="CHARGE")

        if updated == current:
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
        result = self._poll_correlation_id(vin, id) if verify else id

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
