"""The colour-depth switch.

The renderer draws antialiased and the panel has four grey levels, so 4-colour
is the default. Turning this off sends a 1bpp frame instead: half the bytes,
and hard-edged glyphs that some people prefer on a panel this small.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import EPaperConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EPaperConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the single colour-depth switch."""
    async_add_entities([EPaperGray4Switch(entry.runtime_data)])


class EPaperGray4Switch(RestoreEntity, SwitchEntity):
    """Whether frames are sent as 4 greys or black and white."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "gray4"

    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.address}_gray4"
        self._attr_device_info = coordinator.device_info

    @property
    def is_on(self) -> bool:
        return self.coordinator.gray4

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_gray4(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self.coordinator.async_set_gray4(False)

    async def async_added_to_hass(self) -> None:
        """Restore the depth without repainting a panel that already matches."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )
        if (last := await self.async_get_last_state()) is not None:
            self.coordinator.async_restore_gray4(last.state == "on")
