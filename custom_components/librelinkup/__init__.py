from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError

from .const import CONF_PATIENT_ID, DOMAIN, PLATFORMS
from .coordinator import LibreLinkUpCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    if not entry.data.get(CONF_PATIENT_ID):
        # Retrying cannot fix this, and guessing a connection could attach the
        # entry to the wrong person, so fail permanently and let the user
        # re-add the entry.
        raise ConfigEntryError(
            "This LibreLinkUp entry has no person selected. "
            "Remove it and set it up again."
        )

    coordinator = LibreLinkUpCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
