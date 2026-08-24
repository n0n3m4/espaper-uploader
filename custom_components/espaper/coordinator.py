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
from homeassistant.util import dt as dt_util

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


class EPaperCoordinator:
    """Owns the desired text, the panel's actual text, and the gap between."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, address: str) -> None:
        self.hass = hass
        self.entry = entry
        self.address = address
        self.display = EPaperDisplay(address)

        self.markdown = ""
        self.uploaded_markdown: str | None = None
        self.status = STATUS_IDLE
        self.last_error: str | None = None
        self.last_upload: datetime | None = None

        self._device: BLEDevice | None = None
        self._task: asyncio.Task | None = None
        self._listeners: list[Callable[[], None]] = []
        self._unsubs: list[Callable[[], None]] = []

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

    async def async_set_markdown(self, text: str) -> None:
        """Set the text to display, and try to push it."""
        if text == self.markdown:
            return
        self.markdown = text
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
        """The panel is awake: take the chance if we owe it a frame."""
        self._device = service_info.device
        self._maybe_upload()

    @callback
    def _maybe_upload(self) -> None:
        if self.status != STATUS_PENDING or self._device is None:
            return
        if self._task is not None and not self._task.done():
            return
        self._task = self.entry.async_create_background_task(
            self.hass, self._async_upload(), f"espaper upload {self.address}"
        )

    async def _async_upload(self) -> None:
        """One upload attempt for whatever the text says right now."""
        sending = self.markdown
        device = self._device
        assert device is not None
        self.status = STATUS_UPLOADING
        self._notify()

        text = expand_escapes(sending)
        try:
            await self.display.push(
                device, lambda w, h: render_markdown(text, (w, h))
            )
        except (EPaperError, TimeoutError, OSError) as err:
            # Stay pending: the next advertisement is another chance. The
            # board's own duty cycle rate-limits retries to one per wake.
            _LOGGER.debug("espaper %s: upload failed: %s", self.address, err)
            self.last_error = str(err)
            self.status = STATUS_PENDING
        else:
            self.uploaded_markdown = sending
            self.last_error = None
            self.last_upload = dt_util.utcnow()
            # Anything typed mid-upload is still owed to the panel; otherwise
            # this is the latch that stops us connecting on every wake.
            self.status = STATUS_IDLE if self.markdown == sending else STATUS_PENDING

        self._notify()
        if self.status == STATUS_PENDING and self.markdown == sending:
            return  # failed: wait for the next advertisement rather than spin
        # Clear first: _maybe_upload's in-flight guard would otherwise see
        # this very task, which is still running, and refuse to re-kick.
        self._task = None
        self._maybe_upload()

    @callback
    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()
