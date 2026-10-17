"""Base Entity for Uconnect integration."""

from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity import DeviceInfo

from py_uconnect.client import Vehicle

from .const import DOMAIN
from .coordinator import UconnectDataUpdateCoordinator


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

        return DeviceInfo(
            identifiers={(DOMAIN, self.vehicle.vin)},
            manufacturer=self.vehicle.make,
            model=self.vehicle.model,
            name=f"{self.vehicle.make} {self.vehicle.nickname or self.vehicle.model}",
        )
