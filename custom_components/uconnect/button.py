"""Buttons for Uconnect integration."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Final

from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntityDescription,
    ButtonEntity,
)

from .pyuconnect.client import Vehicle
from .pyuconnect.command import (
    COMMAND_CHARGE,
    COMMAND_DEEP_REFRESH,
    COMMAND_LIGHTS,
    COMMAND_LIGHTS_HORN,
    COMMAND_REFRESH_LOCATION,
    Command,
)

from .const import DOMAIN, CONF_ADD_COMMAND_ENTITIES
from .coordinator import UconnectDataUpdateCoordinator
from .entity import UconnectCommandEntity, UconnectEntity, was_added


@dataclass(frozen=True, kw_only=True)
class UconnectButtonEntityDescription(ButtonEntityDescription):
    """A class that describes custom switch entities."""

    command: Command | None = None


BUTTON_DESCRIPTIONS: Final[tuple[UconnectButtonEntityDescription, ...]] = (
    # Refreshes only fetch data, so they sit with the diagnostics and are not
    # pressed along with the buttons of an area
    UconnectButtonEntityDescription(
        key="button_location",
        name="Refresh Location",
        icon="mdi:crosshairs-gps",
        command=COMMAND_REFRESH_LOCATION,
        device_class=ButtonDeviceClass.UPDATE,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    UconnectButtonEntityDescription(
        key="button_deep_refresh",
        name="Deep Refresh",
        icon="mdi:refresh",
        command=COMMAND_DEEP_REFRESH,
        device_class=ButtonDeviceClass.UPDATE,
        entity_category=EntityCategory.DIAGNOSTIC,
    ),
    UconnectButtonEntityDescription(
        key="button_charge",
        name="Charge Now",
        icon="mdi:ev-station",
        command=COMMAND_CHARGE,
    ),
    UconnectButtonEntityDescription(
        key="button_lights",
        name="Lights",
        icon="mdi:car-light-dimmed",
        command=COMMAND_LIGHTS,
        device_class=ButtonDeviceClass.IDENTIFY,
    ),
    UconnectButtonEntityDescription(
        key="button_lights_horn",
        name="Lights & Horn",
        icon="mdi:bugle",
        command=COMMAND_LIGHTS_HORN,
        device_class=ButtonDeviceClass.IDENTIFY,
    ),
)


# Commands go to one vehicle at a time
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: UconnectDataUpdateCoordinator = config_entry.runtime_data
    entities: list[ButtonEntity] = []

    # EV-specific buttons (always available)
    for vehicle in coordinator.client.vehicles.values():
        if getattr(vehicle, "state_of_charge", None) is not None or was_added(
            hass, "button", vehicle.vin, "reset_battery_learning"
        ):
            entities.append(UconnectResetLearningButton(coordinator, vehicle))

    # Command entities (gated behind option)
    if config_entry.options.get(CONF_ADD_COMMAND_ENTITIES):
        for vehicle in coordinator.client.vehicles.values():
            entities.append(UconnectButtonUpdate(coordinator, vehicle))

            for description in BUTTON_DESCRIPTIONS:
                if (
                    description.command is not None
                    and description.command.name in vehicle.supported_commands
                ) or was_added(hass, "button", vehicle.vin, description.key):
                    entities.append(UconnectButton(coordinator, description, vehicle))

    async_add_entities(entities)


class UconnectButton(ButtonEntity, UconnectCommandEntity):
    def __init__(
        self,
        coordinator: UconnectDataUpdateCoordinator,
        description: UconnectButtonEntityDescription,
        vehicle: Vehicle,
    ):
        UconnectCommandEntity.__init__(self, coordinator, vehicle)

        self.entity_description: UconnectButtonEntityDescription = description
        self._attr_name = (
            f"{vehicle.make} {vehicle.nickname or vehicle.model} {description.name}"
        )
        self._attr_unique_id = f"{DOMAIN}_{vehicle.vin}_{description.key}"

    @property
    def icon(self):
        return self.entity_description.icon

    async def async_press(self, **kwargs):
        try:
            await self.coordinator.async_command(
                self.vehicle.vin, self.entity_description.command
            )
        except HomeAssistantError:
            raise
        except Exception as err:
            raise HomeAssistantError(f"Command failed: {err}") from err


class UconnectButtonUpdate(ButtonEntity, UconnectEntity):
    def __init__(
        self,
        coordinator: UconnectDataUpdateCoordinator,
        vehicle: Vehicle,
    ):
        UconnectEntity.__init__(self, coordinator, vehicle)

        self._attr_name = (
            f"{vehicle.make} {vehicle.nickname or vehicle.model} Update Data"
        )
        self._attr_unique_id = f"{DOMAIN}_{vehicle.vin}_update"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def icon(self):
        return "mdi:update"

    async def async_press(self, **kwargs):
        await self.coordinator.async_refresh()


class UconnectResetLearningButton(ButtonEntity, UconnectEntity):
    """Button to reset learned battery charging and drain parameters."""

    def __init__(
        self,
        coordinator: UconnectDataUpdateCoordinator,
        vehicle: Vehicle,
    ):
        UconnectEntity.__init__(self, coordinator, vehicle)

        self._attr_name = (
            f"{vehicle.make} {vehicle.nickname or vehicle.model} Reset Battery Learning"
        )
        self._attr_unique_id = f"{DOMAIN}_{vehicle.vin}_reset_battery_learning"
        self._attr_icon = "mdi:head-sync-outline"
        self._attr_entity_category = EntityCategory.CONFIG

    async def async_press(self, **kwargs):
        sensor = self.coordinator.extrapolated_soc_sensors.get(self._vin)
        if sensor is None:
            raise HomeAssistantError(
                "The extrapolated battery sensor is disabled, nothing to reset"
            )
        sensor.reset_learning()
