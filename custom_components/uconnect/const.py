"""Constants for the Uconnect integration"""

from datetime import timedelta

from .pyuconnect.brands import (
    ALFA_ROMEO_ASIA,
    ALFA_ROMEO_EU,
    ALFA_ROMEO_US_CANADA,
    CHRYSLER_CANADA,
    CHRYSLER_US,
    DODGE_US,
    FIAT_ASIA,
    FIAT_CANADA,
    FIAT_EU,
    FIAT_US,
    JEEP_ASIA,
    JEEP_EU,
    JEEP_US,
    MASERATI_ASIA,
    MASERATI_EU,
    MASERATI_US_CANADA,
    RAM_US,
)

DOMAIN: str = "uconnect"

# Fired once for each placeholder status the cloud sends, a state of charge
# of 0 next to a range, whose state of charge and times to full are left out
EVENT_PLACEHOLDER_DISCARDED: str = f"{DOMAIN}_placeholder_discarded"

CONF_BRAND_REGION: str = "brand_region"
CONF_DISABLE_TLS_VERIFICATION: str = "disable_tls_verification"
CONF_ADD_COMMAND_ENTITIES: str = "add_command_entities"
CONF_CHARGING_REFRESH_INTERVAL: str = "charging_refresh_interval"
CONF_UNLOCK_REQUIRES_PIN: str = "unlock_requires_pin"

BRANDS = {
    1: FIAT_EU.name,
    2: FIAT_US.name,
    3: RAM_US.name,
    4: DODGE_US.name,
    5: JEEP_EU.name,
    6: JEEP_US.name,
    7: MASERATI_ASIA.name,
    8: MASERATI_EU.name,
    9: MASERATI_US_CANADA.name,
    10: CHRYSLER_CANADA.name,
    11: CHRYSLER_US.name,
    12: ALFA_ROMEO_ASIA.name,
    13: ALFA_ROMEO_EU.name,
    14: ALFA_ROMEO_US_CANADA.name,
    15: FIAT_ASIA.name,
    16: FIAT_CANADA.name,
    17: JEEP_ASIA.name,
}

DEFAULT_PIN: str = ""
DEFAULT_SCAN_INTERVAL: int = 5
DEFAULT_CHARGING_REFRESH_INTERVAL: int = 30
MIN_CHARGING_REFRESH_INTERVAL: int = 15

# Unlocking asks for the Uconnect PIN as its code unless turned off
DEFAULT_UNLOCK_REQUIRES_PIN: bool = True
UNLOCK_COMMANDS: frozenset[str] = frozenset({"RDU", "ROTRUNKUNLOCK", "ROLIFTGATEUNLOCK"})
# Wrong codes in a row after which unlocking is refused for a while
UNLOCK_MAX_WRONG_CODES: int = 5
UNLOCK_LOCKOUT: timedelta = timedelta(minutes=5)

# Failed polls in a row after which entities turn unavailable
MAX_CONSECUTIVE_FAILURES: int = 3

# How often the health report and maintenance history are fetched
SLOW_EXTRA_DATA_INTERVAL: timedelta = timedelta(hours=6)

# While the vehicle is on it is polled this often, unless the interval set is
# shorter. Once it is switched off it is polled again after each of these
# delays: the upload with the switch-off and the position mostly comes minutes
# after the one saying it is off.
DRIVING_SCAN_INTERVAL: timedelta = timedelta(minutes=2)
SWITCH_OFF_POLL_DELAYS: tuple[timedelta, ...] = (
    timedelta(minutes=1),
    timedelta(minutes=3),
)
# A position still older than the switch-off after the last of those polls is
# asked for once, which wakes the vehicle, but only this soon after it
SWITCH_OFF_LOCATION_WINDOW: timedelta = timedelta(minutes=10)
# The position that comes with the switch-off is stamped up to a minute and a
# half before it, so one this much older still counts as having come along
SWITCH_OFF_LOCATION_MARGIN: timedelta = timedelta(minutes=2)

UNIT_DYNAMIC: str = "dynamic"
