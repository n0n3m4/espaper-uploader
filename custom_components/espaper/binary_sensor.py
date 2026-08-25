"""Whether the panel is still around.

Not whether it is awake -- it is awake about two seconds a minute, and a sensor
that spent 97% of its life "off" would be useless. This is the slower question:
has the panel been heard from recently enough that a frame sent now will get
there. It is also what stops the coordinator connecting to a panel that is in a
drawer, which on an ESPHome proxy costs a connection slot everything else on it
is queueing for.
"""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import EPaperConfigEntry


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EPaperConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the single connectivity sensor."""
    async_add_entities([EPaperConnectivity(entry.runtime_data)])


class EPaperConnectivity(BinarySensorEntity):
    """On while the panel has been seen inside the last few wake cycles."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "connectivity"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.address}_connectivity"
        self._attr_device_info = coordinator.device_info

    @property
    def is_on(self) -> bool:
        return self.coordinator.online

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )
