"""Serve the Lovelace card that ships inside this integration.

Registering the card here, rather than asking the user to add a Lovelace
resource by hand, is what lets `www/espaper-card.js` travel with the
integration: HACS installs the whole `custom_components/espaper` directory, so
the card arrives with everything else and works straight away.

It is registered *as* a Lovelace resource, though, not with
``frontend.add_extra_js_url``. The two look interchangeable and are not: the
frontend loads and awaits its resources before it builds any card, while an
extra JS URL is only a ``<script type="module">`` injected into the page, which
races card creation and loses on a cold load -- the card element is not defined
yet, so Home Assistant renders "Configuration error" instead. Worse, a page the
frontend's service worker served from cache never carries the tag at all, which
is why the card would appear only after something forced a soft reload.

Resources are storage-mode only. A YAML-mode dashboard takes its resource list
from configuration.yaml, so there the extra JS URL is still the best available
answer.
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
LOVELACE = "lovelace"

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

    # In the executor: HA runs the very same isdir check off-loop inside
    # async_register_static_paths.
    if not await hass.async_add_executor_job(CARD_PATH.is_file):
        # Worth a warning rather than a 404 later: this means the install is
        # incomplete (an old HACS download, or a hand-copy that missed www/),
        # and the symptom -- "custom element doesn't exist" -- points nowhere
        # near the cause.
        _LOGGER.warning(
            "espaper: %s is missing, so the dashboard card will not load; "
            "re-download the integration",
            CARD_PATH,
        )
        return

    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(CARD_PATH), cache_headers=False)]
    )
    # Cache-bust from the manifest rather than a constant kept alongside it: a
    # second copy of the version drifts on release and serves a stale card.
    version = (await async_get_integration(hass, DOMAIN)).version
    url = f"{CARD_URL}?v={version}"

    try:
        registered = await _async_register_resource(hass, url)
    except Exception:  # noqa: BLE001
        # Every attribute reached for below is Home Assistant's own internals,
        # which move between versions. Falling back to the script tag is worse
        # but not fatal, and a broken card beats a config entry that will not
        # set up.
        _LOGGER.exception("espaper: could not add the card as a Lovelace resource")
        registered = False

    if not registered:
        frontend.add_extra_js_url(hass, url)
    _LOGGER.info(
        "espaper: dashboard card served at %s (%s)",
        url,
        "Lovelace resource" if registered else "extra JS URL",
    )


async def _async_register_resource(hass: HomeAssistant, url: str) -> bool:
    """Put the card in Lovelace's resource list. True if it is in there now.

    Idempotent across restarts and upgrades: the stored URL carries the version,
    so on an upgrade the existing entry is rewritten rather than duplicated.
    """
    lovelace = hass.data.get(LOVELACE)
    resources = getattr(lovelace, "resources", None)
    if resources is None or getattr(lovelace, "resource_mode", None) != "storage":
        return False

    # Loads the collection if it has not been read yet; the resource websocket
    # API does the same thing before touching async_items().
    await resources.async_get_info()

    for item in resources.async_items():
        if str(item.get("url", "")).startswith(CARD_URL):
            if item["url"] != url:
                await resources.async_update_item(item["id"], {"url": url})
                _LOGGER.debug("espaper: card resource updated to %s", url)
            return True

    await resources.async_create_item({"res_type": "module", "url": url})
    _LOGGER.debug("espaper: card resource created for %s", url)
    return True
