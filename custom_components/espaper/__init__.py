"""The ESPaper uploader integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant

from .coordinator import EPaperCoordinator

PLATFORMS = [Platform.TEXT]

type EPaperConfigEntry = ConfigEntry[EPaperCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: EPaperConfigEntry) -> bool:
    """Set up an ESPaper display from a config entry."""
    coordinator = EPaperCoordinator(hass, entry, entry.data[CONF_ADDRESS])
    await coordinator.async_start()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EPaperConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        await entry.runtime_data.async_stop()
    return unload_ok
