"""The Markdown text entity, and the service that bypasses its length cap."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.text import TextEntity, TextMode
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, entity_platform
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import EPaperConfigEntry
from .const import ATTR_MARKDOWN, DOMAIN, MANUFACTURER, MODEL, SERVICE_SET_MARKDOWN

# Home Assistant truncates any entity state at 255 characters, so this is the
# ceiling for the UI text box. Longer documents go through set_markdown and
# live in the full_markdown attribute, which has no such cap.
MAX_STATE_LENGTH = 255
ATTR_FULL_MARKDOWN = "full_markdown"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EPaperConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the single text entity, and register the service on it."""
    entity_platform.async_get_current_platform().async_register_entity_service(
        SERVICE_SET_MARKDOWN,
        {vol.Required(ATTR_MARKDOWN): cv.string},
        "async_set_markdown",
    )
    async_add_entities([EPaperText(entry.runtime_data)])


class EPaperText(RestoreEntity, TextEntity):
    """What the panel should be showing, as editable text."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "markdown"
    _attr_mode = TextMode.TEXT
    _attr_native_max = MAX_STATE_LENGTH
    _attr_native_min = 0

    def __init__(self, coordinator) -> None:
        self.coordinator = coordinator
        self._attr_unique_id = f"{coordinator.address}_markdown"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.address)},
            connections={(CONNECTION_BLUETOOTH, coordinator.address)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=f"{MANUFACTURER} {coordinator.address}",
        )

    @property
    def native_value(self) -> str:
        """The Markdown, truncated to what a state is allowed to hold."""
        return self.coordinator.markdown[:MAX_STATE_LENGTH]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "upload_status": self.coordinator.status,
            "last_error": self.coordinator.last_error,
            "last_upload": self.coordinator.last_upload,
            # Also the restore path for text longer than a state can hold.
            ATTR_FULL_MARKDOWN: self.coordinator.markdown,
            "uploaded_markdown": self.coordinator.uploaded_markdown,
        }

    async def async_set_value(self, value: str) -> None:
        """Handle an edit from the UI."""
        await self.coordinator.async_set_markdown(value)

    async def async_set_markdown(self, markdown: str) -> None:
        """Service target: same path, but no length ceiling."""
        await self.coordinator.async_set_markdown(markdown)

    async def async_added_to_hass(self) -> None:
        """Restore the text and hand it back to the coordinator."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self.coordinator.async_add_listener(self.async_write_ha_state)
        )
        if (last := await self.async_get_last_state()) is None:
            return
        attrs = last.attributes
        self.coordinator.async_restore(
            # The attribute holds the untruncated text; the state is a fallback
            # for entries written before the attribute existed.
            attrs.get(ATTR_FULL_MARKDOWN) or last.state or "",
            attrs.get("uploaded_markdown"),
        )
