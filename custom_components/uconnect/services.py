"""Actions for the Uconnect integration."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr

from .pyuconnect.command import (
    COMMAND_CABIN_VENTILATION,
    COMMAND_CHARGE,
    COMMAND_CHARGE_V4,
    COMMAND_COMFORT_OFF,
    COMMAND_COMFORT_ON,
    COMMAND_DEEP_REFRESH,
    COMMAND_DEEP_REFRESH_V2,
    COMMAND_DOORS_LOCK,
    COMMAND_DOORS_UNLOCK,
    COMMAND_ENGINE_OFF,
    COMMAND_ENGINE_ON,
    COMMAND_HVAC_OFF,
    COMMAND_HVAC_ON,
    COMMAND_LIFTGATE_LOCK,
    COMMAND_LIFTGATE_UNLOCK,
    COMMAND_LIGHTS,
    COMMAND_LIGHTS_HORN,
    COMMAND_PRECOND_OFF,
    COMMAND_PRECOND_ON,
    COMMAND_REFRESH_LOCATION,
    COMMAND_TRUNK_LOCK,
    COMMAND_TRUNK_UNLOCK,
    Command,
)

from .charge_schedule import WEEKDAYS, format_time, supports_emea_schedules
from .const import DOMAIN
from .coordinator import UconnectDataUpdateCoordinator

SERVICE_UPDATE = "update"

SERVICE_ENGINE_ON = "engine_on"
SERVICE_ENGINE_OFF = "engine_off"
SERVICE_COMFORT_ON = "comfort_on"
SERVICE_COMFORT_OFF = "comfort_off"
SERVICE_HVAC_ON = "hvac_on"
SERVICE_HVAC_OFF = "hvac_off"
SERVICE_PRECOND_ON = "precond_on"
SERVICE_PRECOND_OFF = "precond_off"
SERVICE_LIGHTS_HORN = "lights_horn"
SERVICE_LIGHTS = "lights"
SERVICE_DOORS_UNLOCK = "doors_unlock"
SERVICE_DOORS_LOCK = "doors_lock"
SERVICE_TRUNK_UNLOCK = "trunk_unlock"
SERVICE_TRUNK_LOCK = "trunk_lock"
SERVICE_LIFTGATE_UNLOCK = "liftgate_unlock"
SERVICE_LIFTGATE_LOCK = "liftgate_lock"
SERVICE_CABIN_VENTILATION = "cabin_ventilation"
SERVICE_CHARGE = "charge_now"
SERVICE_CHARGE_V4 = "charge_now_v4"
SERVICE_DEEP_REFRESH = "deep_refresh"
SERVICE_DEEP_REFRESH_V2 = "deep_refresh_v2"
SERVICE_REFRESH_LOCATION = "refresh_location"
SERVICE_SET_CHARGE_SCHEDULE = "set_charge_schedule"
SERVICE_SET_CHARGE_SCHEDULE_SLOT = "set_charge_schedule_slot"

SUPPORTED_SERVICES = [
    SERVICE_UPDATE,
    SERVICE_ENGINE_ON,
    SERVICE_ENGINE_OFF,
    SERVICE_COMFORT_ON,
    SERVICE_COMFORT_OFF,
    SERVICE_HVAC_ON,
    SERVICE_HVAC_OFF,
    SERVICE_PRECOND_ON,
    SERVICE_PRECOND_OFF,
    SERVICE_LIGHTS_HORN,
    SERVICE_LIGHTS,
    SERVICE_DOORS_UNLOCK,
    SERVICE_DOORS_LOCK,
    SERVICE_TRUNK_UNLOCK,
    SERVICE_TRUNK_LOCK,
    SERVICE_LIFTGATE_UNLOCK,
    SERVICE_LIFTGATE_LOCK,
    SERVICE_CABIN_VENTILATION,
    SERVICE_CHARGE,
    SERVICE_CHARGE_V4,
    SERVICE_DEEP_REFRESH,
    SERVICE_DEEP_REFRESH_V2,
    SERVICE_REFRESH_LOCATION,
]

SERVICES_COMMANDS: dict[str, Command] = {
    SERVICE_ENGINE_ON: COMMAND_ENGINE_ON,
    SERVICE_ENGINE_OFF: COMMAND_ENGINE_OFF,
    SERVICE_COMFORT_ON: COMMAND_COMFORT_ON,
    SERVICE_COMFORT_OFF: COMMAND_COMFORT_OFF,
    SERVICE_HVAC_ON: COMMAND_HVAC_ON,
    SERVICE_HVAC_OFF: COMMAND_HVAC_OFF,
    SERVICE_PRECOND_ON: COMMAND_PRECOND_ON,
    SERVICE_PRECOND_OFF: COMMAND_PRECOND_OFF,
    SERVICE_LIGHTS_HORN: COMMAND_LIGHTS_HORN,
    SERVICE_LIGHTS: COMMAND_LIGHTS,
    SERVICE_DOORS_UNLOCK: COMMAND_DOORS_UNLOCK,
    SERVICE_DOORS_LOCK: COMMAND_DOORS_LOCK,
    SERVICE_TRUNK_UNLOCK: COMMAND_TRUNK_UNLOCK,
    SERVICE_TRUNK_LOCK: COMMAND_TRUNK_LOCK,
    SERVICE_LIFTGATE_UNLOCK: COMMAND_LIFTGATE_UNLOCK,
    SERVICE_LIFTGATE_LOCK: COMMAND_LIFTGATE_LOCK,
    SERVICE_CABIN_VENTILATION: COMMAND_CABIN_VENTILATION,
    SERVICE_CHARGE: COMMAND_CHARGE,
    SERVICE_CHARGE_V4: COMMAND_CHARGE_V4,
    SERVICE_DEEP_REFRESH: COMMAND_DEEP_REFRESH,
    SERVICE_DEEP_REFRESH_V2: COMMAND_DEEP_REFRESH_V2,
    SERVICE_REFRESH_LOCATION: COMMAND_REFRESH_LOCATION,
}


DEVICE_SCHEMA = vol.Schema({vol.Optional(ATTR_DEVICE_ID): cv.string})

COMMAND_SCHEMA = DEVICE_SCHEMA

SET_CHARGE_SCHEDULE_SCHEMA = DEVICE_SCHEMA.extend(
    {vol.Required("schedule"): dict}
)

SET_CHARGE_SCHEDULE_SLOT_SCHEMA = DEVICE_SCHEMA.extend(
    {
        vol.Required("slot"): vol.All(vol.Coerce(int), vol.Range(min=1, max=3)),
        vol.Optional("enabled"): cv.boolean,
        vol.Optional("start_time"): cv.time,
        vol.Optional("end_time"): cv.time,
        vol.Optional("days"): vol.All(cv.ensure_list, [vol.In(WEEKDAYS)]),
        vol.Optional("charge_to_full"): cv.boolean,
        vol.Optional("cabin_priority"): cv.boolean,
    }
)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions once for the integration.

    They exist whether or not an entry is loaded, so automations that use
    them validate even while the cloud is unreachable at startup. Whether
    the targeted vehicle supports an action is checked when it is called.
    """

    async def async_call_command(call: ServiceCall) -> None:
        coordinator, vin = _resolve_target(hass, call)

        if call.service == SERVICE_UPDATE:
            await coordinator.async_refresh()
            return

        cmd = SERVICES_COMMANDS[call.service]
        if cmd.name not in coordinator.client.vehicles[vin].supported_commands:
            raise ServiceValidationError(
                f"Action {call.service} is not supported by vehicle {vin}"
            )
        await coordinator.async_command(vin, cmd)

    async def async_set_charge_schedule(call: ServiceCall) -> None:
        coordinator, vin = _resolve_target(hass, call)
        await coordinator.async_set_charge_schedule(vin, call.data["schedule"])

    async def async_set_charge_schedule_slot(call: ServiceCall) -> None:
        coordinator, vin = _resolve_target(hass, call)
        if not supports_emea_schedules(
            coordinator.client, coordinator.client.vehicles[vin]
        ):
            raise ServiceValidationError(
                f"Vehicle {vin} does not report charge schedule slots"
            )

        changes = {}
        if "enabled" in call.data:
            changes["enable"] = call.data["enabled"]
        for field in ("start_time", "end_time"):
            if field in call.data:
                changes[field] = format_time(call.data[field])
        if "days" in call.data:
            changes["days"] = list(call.data["days"])
        for field in ("charge_to_full", "cabin_priority"):
            if field in call.data:
                changes[field] = call.data[field]

        if not changes:
            raise ServiceValidationError("Nothing to change in the charge schedule")

        await coordinator.async_set_emea_charge_schedule(
            vin, call.data["slot"] - 1, **changes
        )

    for service in SUPPORTED_SERVICES:
        hass.services.async_register(
            DOMAIN, service, async_call_command, schema=COMMAND_SCHEMA
        )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_CHARGE_SCHEDULE,
        async_set_charge_schedule,
        schema=SET_CHARGE_SCHEDULE_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_CHARGE_SCHEDULE_SLOT,
        async_set_charge_schedule_slot,
        schema=SET_CHARGE_SCHEDULE_SLOT_SCHEMA,
    )


def _resolve_target(
    hass: HomeAssistant, call: ServiceCall
) -> tuple[UconnectDataUpdateCoordinator, str]:
    """Return the coordinator and VIN an action call is meant for.

    Without a device the call is only unambiguous for a single vehicle.
    """

    loaded = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    if not loaded:
        raise ServiceValidationError("Uconnect is not loaded")

    device_id = call.data.get(ATTR_DEVICE_ID)
    if device_id is None:
        vehicles = [
            (entry.runtime_data, vin)
            for entry in loaded
            for vin in entry.runtime_data.client.vehicles
        ]
        if len(vehicles) != 1:
            raise ServiceValidationError(
                "Select the vehicle, the account has more than one"
            )
        return vehicles[0]

    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise ServiceValidationError(f"Device {device_id} not found")

    vin = next(
        (ident for domain, ident in device.identifiers if domain == DOMAIN), None
    )
    # Newer Home Assistant versions tie a device to a single entry and
    # deprecate the set, older ones only have the set
    if entry_id := getattr(device, "config_entry_id", None):
        entry_ids = {entry_id}
    else:
        entry_ids = set(device.config_entries)
    entry = next((e for e in loaded if e.entry_id in entry_ids), None)
    if vin is None or entry is None:
        raise ServiceValidationError(f"Device {device_id} is not a loaded Uconnect vehicle")

    coordinator: UconnectDataUpdateCoordinator = entry.runtime_data
    if vin not in coordinator.client.vehicles:
        raise ServiceValidationError(f"Vehicle {vin} is currently not available")

    return coordinator, vin
