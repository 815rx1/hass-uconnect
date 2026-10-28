"""Diagnostics support for the Uconnect integration."""

from __future__ import annotations

import dataclasses
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_PASSWORD, CONF_PIN, CONF_USERNAME
from homeassistant.core import HomeAssistant

from . import UconnectConfigEntry
from .wake_guard import async_get_wake_guard

# Keys are redacted wherever they appear, also deep inside the raw payloads
TO_REDACT = {
    CONF_USERNAME,
    CONF_PASSWORD,
    CONF_PIN,
    "vin",
    "VIN",
    "nickname",
    "latitude",
    "longitude",
    "altitude",
    "bearing",
    "image_url",
    "vehicleImageURL",
    "preciseImageURL",
    "uid",
    "UID",
    "email",
    "correlationId",
    "licensePlate",
    "location",
    "address",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: UconnectConfigEntry
) -> dict[str, Any]:
    """Return the data a bug report needs, without anything identifying.

    Vehicles are numbered instead of keyed by VIN, and the raw status the
    cloud last returned is included, because that is what shows which
    fields a model reports.
    """

    entry_info = {
        "data": async_redact_data(dict(entry.data), TO_REDACT),
        "options": async_redact_data(dict(entry.options), TO_REDACT),
        "state": str(entry.state),
    }

    # An entry whose setup failed has no data yet, what it has still helps
    if (coordinator := getattr(entry, "runtime_data", None)) is None:
        return {"entry": entry_info, "coordinator": None, "vehicles": []}

    client = coordinator.client
    last_status = getattr(client, "_last_status", None)
    if not isinstance(last_status, dict):
        last_status = {}
    guard = async_get_wake_guard(hass)
    placeholders = getattr(client, "placeholders", None)
    if not isinstance(placeholders, dict):
        placeholders = {}
    last_charges = client.last_charges()
    if not isinstance(last_charges, dict):
        last_charges = {}

    vehicles = []
    for index, (vin, vehicle) in enumerate(client.vehicles.items(), start=1):
        vehicles.append(
            {
                "vehicle": index,
                "parsed": async_redact_data(dataclasses.asdict(vehicle), TO_REDACT),
                "raw_status": async_redact_data(last_status.get(vin, {}), TO_REDACT),
                "health_report": async_redact_data(
                    coordinator.vhr_data.get(vin, {}), TO_REDACT
                ),
                "charge_schedules_v4": async_redact_data(
                    coordinator.charge_schedule_data.get(vin, {}), TO_REDACT
                ),
                "stolen_vehicle_status": async_redact_data(
                    coordinator.svla_data.get(vin, {}), TO_REDACT
                ),
                # When and why the vehicle was woken, to check the limits
                "wake_ups": [
                    {
                        "at": w.at.isoformat(),
                        "command": w.command,
                        "automatic": w.automatic,
                        "confirmed": w.confirmed,
                    }
                    for w in guard.wakes(vin)
                ],
                "charge_session": dataclasses.asdict(guard.session(vin)),
                # The last status whose state of charge was left out as a
                # placeholder of the cloud
                "last_placeholder": placeholders.get(vin),
                # Kept over a restart for a placeholder answered first
                "last_real_charge": last_charges.get(vin),
            }
        )

    return {
        "entry": entry_info,
        "coordinator": {
            "last_update_success": coordinator.last_update_success,
            "consecutive_failures": coordinator.consecutive_failures,
            "last_successful_update": coordinator.last_successful_update,
            "update_interval": str(coordinator.update_interval),
            "charging_refresh_interval_min": coordinator.charging_refresh_interval,
            "pin_set": bool(client.api.pin),
            "auth_failed": coordinator.auth_failed,
            "unlock_requires_pin": coordinator.unlock_requires_pin,
        },
        "vehicles": vehicles,
    }
