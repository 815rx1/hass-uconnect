"""Switches for Uconnect integration."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Final, Callable

import logging

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.components.switch import (
    SwitchDeviceClass,
    SwitchEntityDescription,
    SwitchEntity,
)

from .pyuconnect.client import Vehicle
from .pyuconnect.command import (
    COMMAND_CHARGE,
    COMMAND_COMFORT_OFF,
    COMMAND_COMFORT_ON,
    COMMAND_ENGINE_OFF,
    COMMAND_ENGINE_ON,
    COMMAND_HVAC_OFF,
    COMMAND_HVAC_ON,
    COMMAND_PRECOND_OFF,
    COMMAND_PRECOND_ON,
    Command,
)

from .charge_schedule import enabled_days
from .const import DOMAIN, CONF_ADD_COMMAND_ENTITIES
from .coordinator import UconnectDataUpdateCoordinator
from .entity import (
    UconnectCommandEntity,
    UconnectEntity,
    schedule_slot_count,
    was_added,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class UconnectSwitchEntityDescription(SwitchEntityDescription):
    """A class that describes custom switch entities."""

    command_on: Command | None = None
    command_off: Command | None = None
    on_icon: str | None = None
    off_icon: str | None = None
    is_on: Callable[[Vehicle], bool | None] | None = None
    is_available: Callable[[Vehicle], bool] = lambda x: True


SWITCH_DESCRIPTIONS: Final[tuple[UconnectSwitchEntityDescription, ...]] = (
    UconnectSwitchEntityDescription(
        key="switch_engine",
        name="Engine",
        on_icon="mdi:engine",
        command_on=COMMAND_ENGINE_ON,
        command_off=COMMAND_ENGINE_OFF,
        is_on=lambda x: getattr(x, "ignition_on", None),
        device_class=SwitchDeviceClass.SWITCH,
    ),
    UconnectSwitchEntityDescription(
        key="switch_precondition",
        name="Precondition",
        on_icon="mdi:air-conditioner",
        command_on=COMMAND_PRECOND_ON,
        command_off=COMMAND_PRECOND_OFF,
        device_class=SwitchDeviceClass.SWITCH,
    ),
    UconnectSwitchEntityDescription(
        key="switch_hvac",
        name="HVAC",
        on_icon="mdi:hvac",
        command_on=COMMAND_HVAC_ON,
        command_off=COMMAND_HVAC_OFF,
        is_on=lambda x: getattr(x, "hvac_on", None),
        device_class=SwitchDeviceClass.SWITCH,
    ),
    UconnectSwitchEntityDescription(
        key="switch_comfort",
        name="Comfort",
        on_icon="mdi:air-conditioner",
        command_on=COMMAND_COMFORT_ON,
        command_off=COMMAND_COMFORT_OFF,
        device_class=SwitchDeviceClass.SWITCH,
    ),
    UconnectSwitchEntityDescription(
        key="switch_charging",
        name="Charging",
        on_icon="mdi:battery-charging",
        off_icon="mdi:battery-alert",
        command_on=COMMAND_CHARGE,
        is_on=lambda x: getattr(x, "charging", None),
        device_class=SwitchDeviceClass.SWITCH,
    ),
    # The trunk is a lock now, see lock.py
)


PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
):
    # Do not add entities if not configured
    if not config_entry.options.get(CONF_ADD_COMMAND_ENTITIES):
        return

    coordinator: UconnectDataUpdateCoordinator = config_entry.runtime_data
    entities: list[UconnectSwitch] = []

    for vehicle in coordinator.client.vehicles.values():
        for description in SWITCH_DESCRIPTIONS:
            supported = any(
                command is not None and command.name in vehicle.supported_commands
                for command in (description.command_on, description.command_off)
            )
            if (
                supported and description.is_available(vehicle)
            ) or was_added(hass, "switch", vehicle.vin, description.key):
                entities.append(UconnectSwitch(coordinator, description, vehicle))

        for slot in range(schedule_slot_count(hass, vehicle)):
            entities.append(
                UconnectChargeScheduleSwitch(coordinator, vehicle, slot)
            )

    async_add_entities(entities)


class UconnectSwitch(SwitchEntity, UconnectCommandEntity):
    """A remote command as a switch.

    Kept out of bulk actions: as configuration entities they are not reached
    by turning off a whole area or device, not bridged to HomeKit by default
    and not exposed to voice assistants by default. "Turn everything off"
    would otherwise wake the vehicle once per switch, and "turn everything
    on" start its engine or climate.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: UconnectDataUpdateCoordinator,
        description: UconnectSwitchEntityDescription,
        vehicle: Vehicle,
    ):
        UconnectCommandEntity.__init__(self, coordinator, vehicle)

        self.entity_description: UconnectSwitchEntityDescription = description
        self._attr_name = (
            f"{vehicle.make} {vehicle.nickname or vehicle.model} {description.name}"
        )
        self._attr_unique_id = f"{DOMAIN}_{vehicle.vin}_{description.key}"

    @property
    def icon(self):
        if self.is_on:
            return self.entity_description.on_icon
        else:
            return self.entity_description.off_icon or self.entity_description.on_icon

    @property
    def is_on(self):
        if self.entity_description.is_on is not None and self.data_current:
            return self.entity_description.is_on(self.vehicle)

        return None

    async def async_turn_on(self, **kwargs):
        if self.entity_description.command_on is None:
            raise ServiceValidationError(
                f"{self.entity_description.name} cannot be turned on"
            )

        # Already reported on: sending it again would only wake the vehicle
        if self.is_on is True:
            _LOGGER.debug("%s is already on, not sending", self.entity_description.key)
            return

        await self._async_send(self.entity_description.command_on, "turn on")

    async def async_turn_off(self, **kwargs):
        # Charging cannot be stopped remotely. Raising here would stop a
        # script that turns off all switches before it reaches its next step,
        # such as arming an alarm, so it is only logged.
        if self.entity_description.command_off is None:
            _LOGGER.warning(
                "%s cannot be turned off remotely, nothing was sent",
                self.entity_description.name,
            )
            self.async_write_ha_state()
            return

        # Unlike turning on, turning off is always sent: a state that is out
        # of date must not leave an engine or the climate running
        await self._async_send(self.entity_description.command_off, "turn off")

    async def _async_send(self, command: Command, what: str) -> None:
        try:
            await self.coordinator.async_command(self.vehicle.vin, command)
        except HomeAssistantError:
            raise
        except Exception as err:
            _LOGGER.error("Failed to %s %s: %s", what, self.entity_description.key, err)
            raise HomeAssistantError(f"Failed to {what}: {err}") from err


class UconnectChargeScheduleSwitch(SwitchEntity, UconnectEntity):
    """Enables or disables one inline charge schedule slot of an EMEA vehicle.

    A configuration entity, so turning off everything in an area does not
    switch off the charging schedules.
    """

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: UconnectDataUpdateCoordinator,
        vehicle: Vehicle,
        slot: int,
    ):
        UconnectEntity.__init__(self, coordinator, vehicle)

        self._slot = slot
        self._attr_name = (
            f"{vehicle.make} {vehicle.nickname or vehicle.model} "
            f"Charge Schedule {slot + 1}"
        )
        self._attr_unique_id = f"{DOMAIN}_{vehicle.vin}_charge_schedule_{slot + 1}"
        self._attr_icon = "mdi:calendar-clock"

    @property
    def _schedule(self):
        schedules = getattr(self.vehicle, "charge_schedules", None) or []
        return schedules[self._slot] if self._slot < len(schedules) else None

    @property
    def available(self) -> bool:
        return super().available and self._schedule is not None

    @property
    def is_on(self):
        schedule = self._schedule
        return schedule.enable if schedule is not None else None

    @property
    def extra_state_attributes(self):
        schedule = self._schedule
        if schedule is None:
            return None

        return {
            "start_time": schedule.start_time,
            "end_time": schedule.end_time,
            "days": enabled_days(schedule),
            "charge_to_full": schedule.charge_to_full,
            "cabin_priority": schedule.cabin_priority,
            "repeat_schedule": schedule.repeat_schedule,
        }

    async def _async_set_enable(self, enable: bool):
        try:
            await self.coordinator.async_set_emea_charge_schedule(
                self._vin, self._slot, enable=enable
            )
        except HomeAssistantError:
            raise
        except Exception as err:
            _LOGGER.error(
                "Failed to set charge schedule %d: %s", self._slot + 1, err
            )
            raise HomeAssistantError(f"Failed to set charge schedule: {err}") from err

    async def async_turn_on(self, **kwargs):
        await self._async_set_enable(True)

    async def async_turn_off(self, **kwargs):
        await self._async_set_enable(False)
