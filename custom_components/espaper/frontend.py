"""Serve the Lovelace card that ships inside this integration.

Registering the card here, rather than asking the user to add a Lovelace
resource, is what lets `www/espaper-card.js` travel with the integration: HACS
installs the whole `custom_components/espaper` directory, so the card arrives
with everything else and works on a storage-mode dashboard straight away.
"""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

CARD_PATH = Path(__file__).parent / "www" / "espaper-card.js"
CARD_URL = f"/{DOMAIN}/espaper-card.js"

_REGISTERED = f"{DOMAIN}_card_registered"


async def async_register_card(hass: HomeAssistant) -> None:
    """Serve the card and load it into the frontend, once per Home Assistant run.

    The guard is on ``hass.data`` rather than the config entry because a second
    panel would otherwise try to claim the same URL, and registering a static
    path twice raises.
    """
    if hass.data.get(_REGISTERED):
        return
    hass.data[_REGISTERED] = True

    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(CARD_PATH), cache_headers=False)]
    )
    # Cache-bust from the manifest rather than a constant kept alongside it: a
    # second copy of the version drifts on release and serves a stale card.
    version = (await async_get_integration(hass, DOMAIN)).version
    frontend.add_extra_js_url(hass, f"{CARD_URL}?v={version}")
    _LOGGER.debug("espaper: card registered at %s", CARD_URL)
