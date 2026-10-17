"""Time entities for Uconnect integration."""

from __future__ import annotations

from datetime import time
import logging

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .pyuconnect.client import Vehicle

from .charge_schedule import format_time, parse_time, supports_emea_schedules
from .const import DOMAIN, CONF_ADD_COMMAND_ENTITIES
from .coordinator import UconnectDataUpdateCoordinator
from .entity import UconnectEntity

_LOGGER = logging.getLogger(__name__)

# Schedule field, entity name suffix, icon
TIME_FIELDS: tuple[tuple[str, str, str], ...] = (
    ("start_time", "Start", "mdi:clock-start"),
    ("end_time", "End", "mdi:clock-end"),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    # Do not add entities if not configured
    if not config_entry.options.get(CONF_ADD_COMMAND_ENTITIES):
        return

    coordinator: UconnectDataUpdateCoordinator = hass.data[DOMAIN][
        config_entry.unique_id
    ]
    entities: list[UconnectChargeScheduleTime] = []
    for vehicle in coordinator.client.vehicles.values():
        if not supports_emea_schedules(coordinator.client, vehicle):
            continue

        for slot in range(len(vehicle.charge_schedules)):
            for field, suffix, icon in TIME_FIELDS:
                entities.append(
                    UconnectChargeScheduleTime(
                        coordinator, vehicle, slot, field, suffix, icon
                    )
                )

    async_add_entities(entities)


class UconnectChargeScheduleTime(TimeEntity, UconnectEntity):
    """Start or end time of one inline charge schedule slot of an EMEA vehicle."""

    def __init__(
        self,
        coordinator: UconnectDataUpdateCoordinator,
        vehicle: Vehicle,
        slot: int,
        field: str,
        suffix: str,
        icon: str,
    ):
        UconnectEntity.__init__(self, coordinator, vehicle)

        self._slot = slot
        self._field = field
        self._attr_name = (
            f"{vehicle.make} {vehicle.nickname or vehicle.model} "
            f"Charge Schedule {slot + 1} {suffix}"
        )
        self._attr_unique_id = (
            f"{DOMAIN}_{vehicle.vin}_charge_schedule_{slot + 1}_{field}"
        )
        self._attr_icon = icon

    @property
    def _schedule(self):
        schedules = getattr(self.vehicle, "charge_schedules", None) or []
        return schedules[self._slot] if self._slot < len(schedules) else None

    @property
    def available(self) -> bool:
        return super().available and self._schedule is not None

    @property
    def native_value(self) -> time | None:
        schedule = self._schedule
        return parse_time(getattr(schedule, self._field)) if schedule else None

    async def async_set_value(self, value: time) -> None:
        try:
            await self.coordinator.async_set_emea_charge_schedule(
                self._vin, self._slot, **{self._field: format_time(value)}
            )
        except Exception as err:
            _LOGGER.error(
                "Failed to set charge schedule %d %s: %s",
                self._slot + 1,
                self._field,
                err,
            )
            raise HomeAssistantError(f"Failed to set charge schedule: {err}") from err
