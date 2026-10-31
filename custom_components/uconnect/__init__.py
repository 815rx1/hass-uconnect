import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_ADD_COMMAND_ENTITIES,
    CONF_UNLOCK_REQUIRES_PIN,
    DEFAULT_UNLOCK_REQUIRES_PIN,
    DOMAIN,
)
from .coordinator import UconnectDataUpdateCoordinator
from .frontend_cards import (
    async_remove_frontend_cards_if_last_entry,
    async_setup_frontend_cards,
)
from .services import async_setup_services
from .wake_guard import async_get_wake_guard, async_setup_wake_guard

_LOGGER = logging.getLogger(__name__)

type UconnectConfigEntry = ConfigEntry[UconnectDataUpdateCoordinator]

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


def _setup_options(options) -> tuple[bool, bool]:
    """The options only applied when the entities are created, as in effect."""
    return (
        bool(options.get(CONF_ADD_COMMAND_ENTITIES)),
        bool(options.get(CONF_UNLOCK_REQUIRES_PIN, DEFAULT_UNLOCK_REQUIRES_PIN)),
    )


VOICE_ASSISTANTS: tuple[str, ...] = (
    "conversation",
    "cloud.alexa",
    "cloud.google_assistant",
)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Load the wake-up history, register the actions and the card.

    They exist independently of the entries: the history is per vehicle,
    actions must validate in automations even while an entry is not loaded,
    and dashboards need the card then too.
    """

    await async_setup_wake_guard(hass)
    async_setup_services(hass)
    # Also here, so dashboards find the card while an entry waits for the
    # cloud to answer at startup
    await async_setup_frontend_cards(hass)
    return True


async def async_setup_entry(
    hass: HomeAssistant, config_entry: UconnectConfigEntry
) -> bool:
    """Set up Uconnect from a config entry."""

    coordinator = UconnectDataUpdateCoordinator(hass, config_entry)
    await coordinator.async_restore_confirmed_states()
    await coordinator.async_restore_last_charges()
    await coordinator.async_restore_pin_rejection()

    # Raises ConfigEntryNotReady on update failures and starts the reauth
    # flow when the credentials were rejected
    await coordinator.async_config_entry_first_refresh()

    config_entry.runtime_data = coordinator
    await async_setup_frontend_cards(hass)
    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)
    _async_unexpose_switches(hass, config_entry)

    applied = _setup_options(config_entry.options)

    async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Apply changed options.

        Intervals and the PIN apply in place. The command entities, and the
        code their locks ask for, are only set up at setup, so changing those
        needs a reload, and turning the entities off has to remove them or
        they linger as unavailable entities.
        """
        wanted = _setup_options(entry.options)
        if wanted == applied:
            await coordinator.update_options(hass, entry)
            return

        if applied[0] and not wanted[0]:
            _async_remove_command_entities(hass, entry)
        hass.config_entries.async_schedule_reload(entry.entry_id)

    config_entry.async_on_unload(config_entry.add_update_listener(_async_options_updated))

    return True


def _async_unexpose_switches(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Take the command switches out of voice assistants, once per entity.

    Switches are exposed to voice assistants by default, and the choice is
    stored per entity, so the configuration category they have now does not
    change it for existing ones. A sentence like "turn off all switches" then
    sent a command to the vehicle per switch, and "turn on" could start its
    engine. Whoever wants them by voice can expose them again, which this
    does not undo.
    """

    try:
        from homeassistant.components.homeassistant.exposed_entities import (
            async_expose_entity,
        )
    except ImportError:
        return

    guard = async_get_wake_guard(hass)
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.domain != Platform.SWITCH:
            continue
        key = f"unexposed:{entity.unique_id}"
        if guard.is_migrated(key):
            continue
        try:
            for assistant in VOICE_ASSISTANTS:
                async_expose_entity(hass, assistant, entity.entity_id, False)
        except Exception as err:
            # Without the voice assistant settings there is nothing to change
            _LOGGER.debug("Could not change the exposure of %s: %s", entity.entity_id, err)
            return
        guard.mark_migrated(key)


def _async_remove_command_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the registry entries of the command entities of an entry."""

    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.domain in COMMAND_PLATFORMS and not entity.unique_id.endswith(
            COMMAND_INDEPENDENT_SUFFIXES
        ):
            registry.async_remove(entity.entity_id)


async def async_unload_entry(
    hass: HomeAssistant, config_entry: UconnectConfigEntry
) -> bool:
    """Unload a config entry."""

    return await hass.config_entries.async_unload_platforms(config_entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> None:
    """Clean up once a config entry has been deleted."""

    await async_remove_frontend_cards_if_last_entry(hass, config_entry)


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: UconnectConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Allow deleting a vehicle that is no longer on the account.

    A vehicle the account still reports would be added again on the next
    poll, so it cannot be deleted. With the entry not loaded nothing would
    add it again, so it can be.
    """

    coordinator = getattr(config_entry, "runtime_data", None)
    if coordinator is None:
        return True

    vehicles = coordinator.client.vehicles
    return not any(
        domain == DOMAIN and identifier in vehicles
        for domain, identifier in device.identifiers
    )
