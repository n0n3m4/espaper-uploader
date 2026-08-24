"""Constants for the ESPaper uploader integration."""

from __future__ import annotations

DOMAIN = "espaper"

MANUFACTURER = "ESPaper"
MODEL = "BLE e-paper display"

SERVICE_SET_MARKDOWN = "set_markdown"
ATTR_MARKDOWN = "markdown"

SERVICE_SET_ROTATION = "set_rotation"
ATTR_ROTATION = "rotation"
ROTATIONS = (0, 90, 180, 270)
