from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .const import CONF_ADD_COMMAND_ENTITIES, DOMAIN
from .coordinator import UconnectDataUpdateCoordinator
from .frontend_cards import (
    async_remove_frontend_cards_if_last_entry,
    async_setup_frontend_cards,
)
from .services import async_setup_services, async_unload_services

PLATFORMS: list[str] = [
    Platform.BINARY_SENSOR,
    Platform.SENSOR,
    Platform.DEVICE_TRACKER,
    Platform.IMAGE,
    Platform.LOCK,
    Platform.SWITCH,
    Platform.BUTTON,
    Platform.SELECT,
    Platform.TIME,
]

# Platforms whose entities only exist with the command entities option. The
# battery learning reset is a button too but is not a command, so it stays.
COMMAND_PLATFORMS: set[str] = {
    Platform.LOCK,
    Platform.SWITCH,
    Platform.BUTTON,
    Platform.SELECT,
    Platform.TIME,
}
COMMAND_INDEPENDENT_SUFFIXES: tuple[str, ...] = ("_reset_battery_learning",)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    return True


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Set up Uconnect from a config entry."""

    coordinator = UconnectDataUpdateCoordinator(hass, config_entry)

    # Raises ConfigEntryNotReady on update failures and starts the reauth
    # flow when the credentials were rejected
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})
    await async_setup_frontend_cards(hass)
    hass.data[DOMAIN][config_entry.unique_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)
    async_setup_services(hass, config_entry)

    command_entities = bool(config_entry.options.get(CONF_ADD_COMMAND_ENTITIES))

    async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Apply changed options.

        Intervals and the PIN apply in place. The command entities are only
        created at setup, so toggling them needs a reload, and turning them
        off has to remove them or they linger as unavailable entities.
        """
        if bool(entry.options.get(CONF_ADD_COMMAND_ENTITIES)) == command_entities:
            await coordinator.update_options(hass, entry)
            return

        if command_entities:
            _async_remove_command_entities(hass, entry)
        hass.config_entries.async_schedule_reload(entry.entry_id)

    config_entry.async_on_unload(config_entry.add_update_listener(_async_options_updated))

    return True


def _async_remove_command_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the registry entries of the command entities of an entry."""

    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.domain in COMMAND_PLATFORMS and not entity.unique_id.endswith(
            COMMAND_INDEPENDENT_SUFFIXES
        ):
            registry.async_remove(entity.entity_id)


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Unload a config entry."""

    domain_data = hass.data.get(DOMAIN, {})

    if unload_ok := await hass.config_entries.async_unload_platforms(
        config_entry, PLATFORMS
    ):
        domain_data.pop(config_entry.unique_id, None)

    if not [k for k in domain_data if not k.startswith("_")]:
        async_unload_services(hass)

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Clean up once a config entry has been deleted."""

    await async_remove_frontend_cards_if_last_entry(hass)
