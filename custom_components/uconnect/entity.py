"""Base Entity for Uconnect integration."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity import DeviceInfo

from .pyuconnect.client import Vehicle

from .charge_schedule import supports_emea_schedules
from .const import DOMAIN
from .coordinator import UconnectDataUpdateCoordinator


def _text(value) -> str | None:
    """The device registry only takes strings, and stops accepting others."""
    return None if value is None else str(value)


def was_added(hass: HomeAssistant, platform: str, vin: str, key: str) -> bool:
    """Return whether the entity of this vehicle and key was created before.

    Entities are created for the values a vehicle reports. A value missing at
    startup, because an endpoint failed or the cloud left a block out, must
    not make an existing entity vanish together with its dashboard cards and
    automations, so registered ones are always created again.
    """

    return (
        er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"{DOMAIN}_{vin}_{key}")
        is not None
    )


def schedule_slot_count(hass: HomeAssistant, vehicle: Vehicle) -> int:
    """Return how many charge schedule slots to create entities for."""

    reported = (
        len(vehicle.charge_schedules) if supports_emea_schedules(None, vehicle) else 0
    )
    registered = sum(
        1
        for slot in range(1, 4)
        if was_added(hass, "switch", vehicle.vin, f"charge_schedule_{slot}")
    )
    return max(reported, registered)


class UconnectEntity(CoordinatorEntity):
    """Class for base entity for Uconnect integration."""

    coordinator: UconnectDataUpdateCoordinator

    def __init__(self, coordinator: UconnectDataUpdateCoordinator, vehicle: Vehicle):
        """Initialize the base entity."""
        super().__init__(coordinator)
        self._vin = vehicle.vin
        self._last_vehicle = vehicle

    @property
    def vehicle(self) -> Vehicle:
        """Get the current vehicle object from the coordinator.

        The library drops a vehicle from its list as soon as one status call
        answers 400 or 404, which happens now and then without the vehicle
        having left the account. The last known object is returned then, so
        properties keep working while the entity reports itself unavailable.
        """
        vehicle = self.coordinator.client.get_vehicles().get(self._vin)
        if vehicle is not None:
            self._last_vehicle = vehicle
        return self._last_vehicle

    @property
    def available(self) -> bool:
        """Return whether the coordinator has fresh data for this vehicle."""
        return super().available and self._vin in self.coordinator.client.get_vehicles()

    @property
    def device_info(self):
        """Return device information to use for this entity."""

        vehicle = self.vehicle
        return DeviceInfo(
            identifiers={(DOMAIN, vehicle.vin)},
            manufacturer=_text(vehicle.make),
            model=_text(vehicle.model),
            name=f"{vehicle.make} {vehicle.nickname or vehicle.model}",
        )


class UconnectCommandEntity(UconnectEntity):
    """An entity that sends a command, available while commands can go out.

    Home Assistant skips an unavailable entity without a word, even one named
    in an action. Commands do not depend on the status poll, so a cloud status
    outage would otherwise silently drop an automation locking the car at
    night. The state it shows is unknown while the data is out of date.
    """

    @property
    def available(self) -> bool:
        coordinator = self.coordinator
        return (
            not coordinator.auth_failed
            and self._vin in coordinator.client.get_vehicles()
        )

    @property
    def data_current(self) -> bool:
        """Whether the last poll worked, so the state shown is current."""
        return self.coordinator.last_update_success
