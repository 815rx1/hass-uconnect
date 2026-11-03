"""Select for Uconnect integration."""

from __future__ import annotations
import logging

from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .pyuconnect.client import Vehicle
from .pyuconnect.api import (
    CHARGING_LEVELS,
    CHARGING_LEVEL_ONE,
    CHARGING_LEVEL_TWO,
    CHARGING_LEVEL_THREE,
    CHARGING_LEVEL_FOUR,
    CHARGING_LEVEL_FIVE,
)

from .charge_schedule import enabled_days
from .const import DOMAIN, CONF_ADD_COMMAND_ENTITIES
from .coordinator import UconnectDataUpdateCoordinator
from .entity import UconnectEntity, schedule_slot_count, was_added

_LOGGER = logging.getLogger(__name__)

CHARGING_LEVEL_NAMES = [x.name for x in CHARGING_LEVELS]

ICONS = {
    CHARGING_LEVEL_ONE.name: "mdi:battery-charging-20",
    CHARGING_LEVEL_TWO.name: "mdi:battery-charging-40",
    CHARGING_LEVEL_THREE.name: "mdi:battery-charging-60",
    CHARGING_LEVEL_FOUR.name: "mdi:battery-charging-80",
    CHARGING_LEVEL_FIVE.name: "mdi:battery-charging-100",
}


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
    entities: list[SelectEntity] = []
    for vehicle in coordinator.client.vehicles.values():
        if getattr(vehicle, "charging_level_preference", None) is not None or was_added(
            hass, "select", vehicle.vin, "charging_level_preference"
        ):
            entities.append(UconnectSelectSetChargingLevel(coordinator, vehicle))

        for slot in range(schedule_slot_count(hass, vehicle)):
            entities.append(UconnectChargeScheduleSelect(coordinator, vehicle, slot))

    async_add_entities(entities)


class UconnectSelectSetChargingLevel(SelectEntity, UconnectEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: UconnectDataUpdateCoordinator,
        vehicle: Vehicle,
    ):
        UconnectEntity.__init__(self, coordinator, vehicle)

        self._attr_unique_id = f"{DOMAIN}_{vehicle.vin}_charging_level_preference"
        self._attr_name = (
            f"{vehicle.make} {vehicle.nickname or vehicle.model} Charging Level Pref"
        )

    @property
    def icon(self):
        return ICONS.get(self.current_option, "mdi:battery-charging")

    @property
    def options(self):
        return CHARGING_LEVEL_NAMES

    @property
    def current_option(self):
        """Return the preference as a level name.

        Older py-uconnect versions report the raw name such as LEVEL_FIVE,
        newer ones map it to the integer scale 1 to 5 used by charging_level.
        """
        preference = self.vehicle.charging_level_preference
        if isinstance(preference, int):
            if 1 <= preference <= len(CHARGING_LEVEL_NAMES):
                return CHARGING_LEVEL_NAMES[preference - 1]
            return None
        return preference

    async def async_select_option(self, option: str) -> None:
        # An automation setting the level on a schedule would otherwise wake
        # the vehicle each time for nothing
        if option == self.current_option:
            _LOGGER.debug(
                "Charging level of %s is already %s, not sent", self.vehicle.vin, option
            )
            return

        try:
            await self.coordinator.async_set_charging_level(self.vehicle.vin, option)
        except HomeAssistantError:
            raise
        except Exception as err:
            _LOGGER.error("Failed to set charging level to %s: %s", option, err)
            raise HomeAssistantError(f"Failed to set charging level: {err}") from err


class UconnectChargeScheduleSelect(SelectEntity, UconnectEntity):
    """Enables or disables one inline charge schedule slot of an EMEA vehicle.

    A select rather than a switch: an action on all switches, such as
    turning everything off at night, reaches configuration entities too, and
    would switch off the charging schedules or turn on the unused slots. The
    states stay on and off as they were. A configuration entity, so it is not
    reached through an area either.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = ["on", "off"]
    _attr_translation_key = "charge_schedule"

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
    def current_option(self):
        schedule = self._schedule
        if schedule is None:
            return None
        return "on" if schedule.enable else "off"

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

    async def async_select_option(self, option: str) -> None:
        try:
            await self.coordinator.async_set_emea_charge_schedule(
                self._vin, self._slot, enable=option == "on"
            )
        except HomeAssistantError:
            raise
        except Exception as err:
            _LOGGER.error(
                "Failed to set charge schedule %d: %s", self._slot + 1, err
            )
            raise HomeAssistantError(f"Failed to set charge schedule: {err}") from err
