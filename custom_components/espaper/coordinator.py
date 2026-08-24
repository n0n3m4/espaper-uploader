"""Renders Markdown and pushes it the next time the panel wakes.

The board advertises for ~2 s and then deep-sleeps ~60 s, so an upload cannot
be scheduled -- it has to be opportunistic. Every advertisement Home Assistant
sees is a chance to connect; the coordinator takes that chance only while an
upload is actually outstanding.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime

from bleak.backends.device import BLEDevice
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .const import DOMAIN, MANUFACTURER, MODEL
from .epaper import EPaperDisplay, EPaperError
from .render import render_markdown

_LOGGER = logging.getLogger(__name__)

def expand_escapes(text: str) -> str:
    r"""Turn a literal ``\n`` into a real newline.

    Home Assistant's text entity is single-line -- ``TextMode`` offers only
    ``text`` and ``password``, and the frontend renders an ``<input>``, so
    there is no way to press Enter in it. Accepting the escape keeps that box
    usable for multi-line notes. Text arriving with real newlines (the
    ``set_markdown`` service, or a YAML block scalar) is untouched.

    The expansion happens here, on the way to the renderer, rather than on the
    way in: the entity keeps showing exactly what was typed, so editing it
    round-trips instead of rewriting itself the first time it is saved.
    """
    return text.replace("\\n", "\n")


STATUS_IDLE = "idle"
STATUS_PENDING = "pending"
STATUS_UPLOADING = "uploading"

# Seconds between attempts while a frame is owed. The board wakes about once a
# minute, and establish_connection keeps the radio listening across a chunk of
# that, so a retry on this cadence lands inside a wake window soon enough
# without holding the adapter busy the whole time.
RETRY_DELAY = 30


class EPaperCoordinator:
    """Owns the desired text, the panel's actual text, and the gap between."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, address: str) -> None:
        self.hass = hass
        self.entry = entry
        self.address = address
        self.display = EPaperDisplay(address)

        self.markdown = ""
        self.uploaded_markdown: str | None = None
        # 4 grey levels rather than pure black and white: the renderer draws
        # antialiased and the panel can show the greys, so this is the default.
        self.gray4 = True
        self.uploaded_gray4: bool | None = None
        self.status = STATUS_IDLE
        self.last_error: str | None = None
        self.last_upload: datetime | None = None

        self._device: BLEDevice | None = None
        self._task: asyncio.Task | None = None
        self._retry_unsub: Callable[[], None] | None = None
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[Callable[[], None]] = []

    @property
    def device_info(self) -> DeviceInfo:
        """The one device every entity of this entry hangs off."""
        return DeviceInfo(
            identifiers={(DOMAIN, self.address)},
            connections={(CONNECTION_BLUETOOTH, self.address)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=f"{MANUFACTURER} {self.address}",
        )

    # ---------------------------------------------------------------- setup

    async def async_start(self) -> None:
        """Subscribe to advertisements from this panel."""
        # Seed with whatever the bluetooth stack already knows, in case the
        # board is awake right now.
        self._device = bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        )
        self._unsubs.append(
            bluetooth.async_register_callback(
                self.hass,
                self._async_on_advertisement,
                bluetooth.BluetoothCallbackMatcher(
                    address=self.address, connectable=True
                ),
                bluetooth.BluetoothScanningMode.ACTIVE,
            )
        )

    async def async_stop(self) -> None:
        """Unsubscribe and cancel any upload in flight."""
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        self._cancel_retry()
        if self._task is not None and not self._task.done():
            self._task.cancel()

    @callback
    def async_add_listener(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Register an entity update listener; returns an unsubscribe callable."""
        self._listeners.append(listener)

        def _remove() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _remove

    # ---------------------------------------------------------------- state

    @callback
    def async_restore(self, markdown: str, uploaded: str | None) -> None:
        """Adopt state recovered from the text entity after a restart.

        If the panel was already showing this text before the restart there is
        nothing to send -- the image is still physically on the display, since
        e-paper holds without power.
        """
        self.markdown = markdown
        self.uploaded_markdown = uploaded
        self.status = STATUS_IDLE if markdown == uploaded else STATUS_PENDING
        self._maybe_upload()

    @callback
    def async_restore_gray4(self, gray4: bool) -> None:
        """Adopt the colour depth recovered from the switch entity.

        Deliberately does not touch the status: the panel is still holding the
        frame it was sent before the restart, so there is nothing to repaint.
        The switch and the text entity restore independently, in either order.
        """
        self.gray4 = self.uploaded_gray4 = gray4

    async def async_set_markdown(self, text: str) -> None:
        """Set the text to display, and try to push it."""
        if text == self.markdown:
            return
        _LOGGER.debug("espaper %s: new markdown set, queueing upload", self.address)
        self.markdown = text
        self.status = STATUS_PENDING
        self.last_error = None
        self._notify()
        self._maybe_upload()

    async def async_set_gray4(self, gray4: bool) -> None:
        """Switch between 4 grey levels and black and white, and repaint."""
        if gray4 == self.gray4:
            return
        _LOGGER.debug(
            "espaper %s: %d colours selected, queueing upload",
            self.address,
            4 if gray4 else 2,
        )
        self.gray4 = gray4
        self.status = STATUS_PENDING
        self.last_error = None
        self._notify()
        self._maybe_upload()

    # --------------------------------------------------------------- upload

    @callback
    def _async_on_advertisement(
        self,
        service_info: bluetooth.BluetoothServiceInfoBleak,
        change: bluetooth.BluetoothChange,
    ) -> None:
        """The panel is awake -- an opportunity, but never the only one.

        This cannot be the retry mechanism. Home Assistant drops an
        advertisement whose name, service UUIDs, service data and manufacturer
        data all match the previous one, and this board advertises a constant
        payload, so after the first sighting this callback essentially stops
        firing. It is kept because it makes the first upload after a text
        change immediate when the panel happens to be awake; _schedule_retry
        is what actually guarantees delivery.
        """
        self._device = service_info.device
        _LOGGER.debug(
            "espaper %s: advertisement seen, status=%s", self.address, self.status
        )
        self._maybe_upload()

    @callback
    def _maybe_upload(self) -> None:
        if self.status != STATUS_PENDING:
            return
        if self._task is not None and not self._task.done():
            _LOGGER.debug("espaper %s: upload already in flight", self.address)
            return
        self._cancel_retry()
        self._task = self.entry.async_create_background_task(
            self.hass, self._async_upload(), f"espaper upload {self.address}"
        )

    @callback
    def _cancel_retry(self) -> None:
        if self._retry_unsub is not None:
            self._retry_unsub()
            self._retry_unsub = None

    @callback
    def _schedule_retry(self) -> None:
        """Try again on a timer, since another advertisement may never come."""
        self._cancel_retry()

        @callback
        def _retry(_now) -> None:
            self._retry_unsub = None
            _LOGGER.debug("espaper %s: retrying upload", self.address)
            self._maybe_upload()

        _LOGGER.debug(
            "espaper %s: retrying in %ds", self.address, RETRY_DELAY
        )
        self._retry_unsub = async_call_later(self.hass, RETRY_DELAY, _retry)

    async def _async_upload(self) -> None:
        """One upload attempt for whatever the text says right now."""
        sending, gray4 = self.markdown, self.gray4
        # Ask the bluetooth stack afresh: the cached device may predate an
        # adapter restart, and there may never have been an advertisement
        # callback to seed it in the first place.
        device = (
            bluetooth.async_ble_device_from_address(
                self.hass, self.address, connectable=True
            )
            or self._device
        )
        if device is None:
            _LOGGER.debug(
                "espaper %s: no BLE device known yet, waiting", self.address
            )
            self.status = STATUS_PENDING
            self._schedule_retry()
            return

        self._device = device
        self.status = STATUS_UPLOADING
        self._notify()
        _LOGGER.debug(
            "espaper %s: uploading %d characters of markdown in %d colours",
            self.address,
            len(sending),
            4 if gray4 else 2,
        )

        text = expand_escapes(sending)
        try:
            await self.display.push(
                device, lambda w, h, g: render_markdown(text, (w, h), g), gray4
            )
        except (EPaperError, TimeoutError, OSError) as err:
            # Expected while the board is in its ~60 s deep sleep: it is only
            # reachable for about two seconds per cycle.
            _LOGGER.debug("espaper %s: upload failed: %s", self.address, err)
            self.last_error = str(err)
            self.status = STATUS_PENDING
        else:
            self.uploaded_markdown = sending
            self.uploaded_gray4 = gray4
            self.last_error = None
            self.last_upload = dt_util.utcnow()
            # Anything changed mid-upload -- text or colour depth -- is still
            # owed to the panel; otherwise this is the latch that stops us
            # connecting on every wake.
            self.status = (
                STATUS_IDLE
                if (self.markdown, self.gray4) == (sending, gray4)
                else STATUS_PENDING
            )
            _LOGGER.debug("espaper %s: upload confirmed by the panel", self.address)

        self._notify()
        # Clear first: _maybe_upload's in-flight guard would otherwise see this
        # very task, which is still running, and refuse to re-kick.
        self._task = None
        if self.status == STATUS_PENDING:
            if (self.markdown, self.gray4) == (sending, gray4):
                self._schedule_retry()  # failed; try again on the timer
            else:
                self._maybe_upload()  # edited mid-upload; send the new frame now

    @callback
    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()
