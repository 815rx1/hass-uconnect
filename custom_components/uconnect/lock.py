"""Lock for Uconnect integration."""

from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Any, Callable, Final

from homeassistant.core import HomeAssistant
from homeassistant.components.lock import LockEntity, LockEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .pyuconnect.client import Vehicle
from .pyuconnect.command import (
    COMMAND_DOORS_LOCK,
    COMMAND_DOORS_UNLOCK,
    COMMAND_LIFTGATE_LOCK,
    COMMAND_LIFTGATE_UNLOCK,
    COMMAND_TRUNK_LOCK,
    COMMAND_TRUNK_UNLOCK,
    Command,
)

from .const import DOMAIN, CONF_ADD_COMMAND_ENTITIES
from .coordinator import UconnectDataUpdateCoordinator
from .entity import UconnectCommandEntity, was_added

_LOGGER = logging.getLogger(__name__)

DOORS: Final[tuple[tuple[str, str], ...]] = (
    ("driver", "door_driver_locked"),
    ("passenger", "door_passenger_locked"),
    ("rear_left", "door_rear_left_locked"),
    ("rear_right", "door_rear_right_locked"),
)

# Lets a lock through without a code, so locking stays one tap. Unlocking
# checks the code itself when the PIN is required, see coordinator.
OPTIONAL_PIN_FORMAT = r"^\d*$"


def doors_locked(vehicle: Vehicle) -> bool | None:
    """Return whether every door the vehicle reports is locked.

    Doors the vehicle does not have, or does not report, are left out. Only
    the driver door was looked at before, which showed the car as locked with
    the passenger door open.
    """

    known = [
        state
        for _, attr in DOORS
        if (state := getattr(vehicle, attr, None)) is not None
    ]
    if not known:
        return None
    return all(known)


@dataclass(frozen=True, kw_only=True)
class UconnectLockEntityDescription(LockEntityDescription):
    """A class that describes custom lock entities."""

    icon_locked: str | None = None
    icon_unlocked: str | None = None
    command_on: Command | None = None
    command_off: Command | None = None
    is_locked: Callable[[Vehicle], bool | None] | None = None


LOCK_DESCRIPTIONS: Final[tuple[UconnectLockEntityDescription, ...]] = (
    UconnectLockEntityDescription(
        key="lock_doors",
        name="Doors Lock",
        icon_locked="mdi:car-door-lock",
        icon_unlocked="mdi:car-door-lock-open",
        command_on=COMMAND_DOORS_LOCK,
        command_off=COMMAND_DOORS_UNLOCK,
        is_locked=doors_locked,
    ),
    # The trunk used to be a switch, so a bulk "turn off" or a voice command
    # for all switches opened it. As a lock it is neither exposed to voice
    # assistants by default nor reached by switch actions.
    UconnectLockEntityDescription(
        key="lock_trunk",
        name="Trunk Lock",
        icon_locked="mdi:lock",
        icon_unlocked="mdi:lock-open-variant",
        command_on=COMMAND_TRUNK_LOCK,
        command_off=COMMAND_TRUNK_UNLOCK,
        is_locked=lambda x: getattr(x, "trunk_locked", None),
    ),
    UconnectLockEntityDescription(
        key="lock_liftgate",
        name="Liftgate Lock",
        icon_locked="mdi:lock",
        icon_unlocked="mdi:lock-open-variant",
        command_on=COMMAND_LIFTGATE_LOCK,
        command_off=COMMAND_LIFTGATE_UNLOCK,
        is_locked=lambda x: getattr(x, "liftgate_locked", None),
    ),
)


PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    # Do not add entities if not configured
    if not config_entry.options.get(CONF_ADD_COMMAND_ENTITIES):
        return

    coordinator: UconnectDataUpdateCoordinator = config_entry.runtime_data
    entities: list[UconnectLock] = []
    for vehicle in coordinator.client.vehicles.values():
        for description in LOCK_DESCRIPTIONS:
            supported = any(
                command is not None and command.name in vehicle.supported_commands
                for command in (description.command_on, description.command_off)
            )
            if supported or was_added(hass, "lock", vehicle.vin, description.key):
                entities.append(UconnectLock(coordinator, description, vehicle))

    async_add_entities(entities)


class UconnectLock(LockEntity, UconnectCommandEntity):
    def __init__(
        self,
        coordinator: UconnectDataUpdateCoordinator,
        description: UconnectLockEntityDescription,
        vehicle: Vehicle,
    ):
        UconnectCommandEntity.__init__(self, coordinator, vehicle)

        self.entity_description: UconnectLockEntityDescription = description
        self._attr_unique_id = f"{DOMAIN}_{vehicle.vin}_{description.key}"
        self._attr_name = (
            f"{vehicle.make} {vehicle.nickname or vehicle.model} {description.name}"
        )
        # Asks for the code in the frontend; the option is applied on reload
        if coordinator.unlock_requires_pin:
            self._attr_code_format = OPTIONAL_PIN_FORMAT

    @property
    def icon(self):
        return (
            self.entity_description.icon_locked
            if self.is_locked
            else self.entity_description.icon_unlocked
        )

    @property
    def is_locked(self):
        if not self.data_current:
            return None
        return self.entity_description.is_locked(self.vehicle)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.key != "lock_doors":
            return None

        def state(value: bool | None) -> str | None:
            if value is None:
                return None
            return "locked" if value else "unlocked"

        return {
            name: state(getattr(self.vehicle, attr, None))
            for name, attr in DOORS
            if getattr(self.vehicle, attr, None) is not None
        }

    async def async_lock(self, **kwargs):
        try:
            await self.coordinator.async_command(
                self.vehicle.vin, self.entity_description.command_on
            )
        except HomeAssistantError:
            raise
        except Exception as err:
            _LOGGER.error("Failed to lock %s: %s", self.vehicle.vin, err)
            raise HomeAssistantError(f"Failed to lock: {err}") from err

    async def async_unlock(self, **kwargs):
        try:
            await self.coordinator.async_command(
                self.vehicle.vin,
                self.entity_description.command_off,
                code=kwargs.get("code"),
            )
        except HomeAssistantError:
            raise
        except Exception as err:
            _LOGGER.error("Failed to unlock %s: %s", self.vehicle.vin, err)
            raise HomeAssistantError(f"Failed to unlock: {err}") from err
