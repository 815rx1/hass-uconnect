"""Helpers for the charge schedules EMEA vehicles report inline.

EMEA vehicles such as the Fiat New 500e do not use the North American
/v4/.../ev/schedule/ endpoint, which answers HTTP 500 for them. Their up to
three schedule slots come with the vehicle status instead and are written
back as a whole with py-uconnect's set_charge_schedule_emea.
"""

from __future__ import annotations

from datetime import time

from .pyuconnect import Client
from .pyuconnect.client import Vehicle

WEEKDAYS: tuple[str, ...] = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


def supports_emea_schedules(client: Client, vehicle: Vehicle) -> bool:
    """Return whether the vehicle reports inline schedules the client can write.

    Library versions without EMEA schedule support have neither the vehicle
    attribute nor the client method, so nothing is set up for them.
    """

    return bool(getattr(vehicle, "charge_schedules", None)) and hasattr(
        client, "set_charge_schedule_emea"
    )


def parse_time(value: str | None) -> time | None:
    """Parse the HH:MM string the API uses into a time."""

    if not value:
        return None

    try:
        hour, minute = value.split(":")[:2]
        return time(int(hour), int(minute))
    except ValueError:
        return None


def format_time(value: time) -> str:
    """Format a time the way the API expects it."""

    return value.strftime("%H:%M")


def enabled_days(schedule) -> list[str]:
    """Return the names of the weekdays a schedule slot is active on."""

    return [day for day in WEEKDAYS if getattr(schedule.days, day, False)]
