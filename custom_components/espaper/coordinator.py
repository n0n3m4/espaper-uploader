"""Renders Markdown and pushes it the next time the panel wakes.

The board advertises for ~2 s and then deep-sleeps ~60 s, so an upload cannot
be scheduled against the panel -- but it can be aimed. A connect attempt is a
20 s net rather than an instant: the controller holds the request pending and
latches onto the peer's first advertisement in hardware. So the coordinator
opens that net a few seconds before the panel is next due, instead of reacting
to an advertisement it has already been told about a second or two too late.

It does that only while an upload is outstanding, and only while the panel is
online -- a connection attempt is not free on an ESPHome Bluetooth proxy, which
has a handful of connection slots and stops scanning while it opens one.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta

from bleak.backends.device import BLEDevice
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.event import async_call_later, async_track_time_interval
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

# Aiming the next attempt. The panel keeps a fixed duty cycle, so the last
# sighting says when the next one is due; the attempt opens LEAD seconds before
# that, and bleak's own 20 s connect timeout keeps it open well past it. LEAD is
# the knob: the net covers a wake from LEAD seconds early to 20 - LEAD seconds
# late, which is what absorbs the panel's clock drift, its render time, and a
# proxy's batching of advertisements.
CYCLE_FALLBACK = 62  # 2 s advertising + 60 s sleep, until HA has learned it
LEAD = 8
# A sighting this fresh means the panel is probably awake this second, so there
# is nothing to wait for.
INSIDE_WINDOW = 3
# ...but never two attempts closer together than this, or a push that fails
# instantly spins.
MIN_DELAY = 2

# Five missed wake cycles. Past this the panel is off, flat or out of range, and
# attempts stop: on an ESPHome proxy each one occupies one of about three
# connection slots and pauses the scanning everything else on it depends on.
# Only the (free) advertisement history is watched, on this cadence.
ONLINE_TTL = 300
OFFLINE_POLL = 60


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
        # Degrees clockwise, for a panel hung on its side or upside down.
        self.rotation = 0
        self.uploaded_rotation: int | None = None
        self.status = STATUS_IDLE
        self.last_error: str | None = None
        self.last_upload: datetime | None = None
        # Whether the panel has been heard from recently enough to be worth
        # connecting to. Not whether it is awake -- it almost never is.
        self.online = False
        self.last_seen: datetime | None = None

        self._cooling_off = False
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
        """Subscribe to advertisements from this panel, and start the clock."""
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
        # Going offline is the passage of time, not an event, and HA suppresses
        # this board's repeat advertisements -- so nothing else would ever
        # notice. Doubles as the backstop for a frame owed to a panel that is
        # not answering.
        self._unsubs.append(
            async_track_time_interval(
                self.hass, self._async_tick, timedelta(seconds=OFFLINE_POLL)
            )
        )
        self._refresh_online()

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
    def async_restore(
        self,
        markdown: str,
        uploaded: str | None,
        rotation: int = 0,
        uploaded_rotation: int | None = None,
    ) -> None:
        """Adopt state recovered from the text entity after a restart.

        If the panel was already showing this text before the restart there is
        nothing to send -- the image is still physically on the display, since
        e-paper holds without power.
        """
        self.markdown = markdown
        self.uploaded_markdown = uploaded
        self.rotation = rotation
        # Entries written before rotation existed have no uploaded_rotation;
        # assume the panel already matches rather than repainting on upgrade.
        self.uploaded_rotation = (
            rotation if uploaded_rotation is None else uploaded_rotation
        )
        self.status = (
            STATUS_IDLE
            if (markdown, self.rotation) == (uploaded, self.uploaded_rotation)
            else STATUS_PENDING
        )
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

    async def async_set_rotation(self, rotation: int) -> None:
        """Turn the layout on the panel, and repaint."""
        if rotation == self.rotation:
            return
        _LOGGER.debug("espaper %s: rotation %d, queueing upload", self.address, rotation)
        self.rotation = rotation
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
        firing. It is kept because when it does fire the panel is awake this
        instant, which beats any prediction; _schedule_retry is what actually
        guarantees delivery.
        """
        self._device = service_info.device
        _LOGGER.debug(
            "espaper %s: advertisement seen, status=%s", self.address, self.status
        )
        # Proof the panel is up this instant, which outranks the cool-off after
        # a failure -- that exists to stop a spin, and a spin does not advertise.
        self._cooling_off = False
        self._refresh_online()
        self._maybe_upload()

    @callback
    def _last_seen(self) -> float | None:
        """Seconds since a connectable scanner last heard this panel.

        ``None`` if there is no sighting at all -- HA drops a device from its
        advertisement history once it has been quiet for long enough, so this
        is also where a panel that has been off for a while ends up.
        """
        info = bluetooth.async_last_service_info(
            self.hass, self.address, connectable=True
        )
        if info is None:
            return None
        return time.monotonic() - info.time

    @callback
    def _refresh_online(self) -> None:
        """Recompute whether the panel counts as present, and say so if it changed."""
        age = self._last_seen()
        if age is not None:
            self.last_seen = dt_util.utcnow() - timedelta(seconds=age)
        online = age is not None and age <= ONLINE_TTL
        if online == self.online:
            return
        self.online = online
        _LOGGER.debug(
            "espaper %s: panel %s", self.address, "online" if online else "offline"
        )
        self._notify()

    @callback
    def _next_attempt_delay(self) -> float | None:
        """Seconds to wait before the attempt most likely to catch the panel.

        ``None`` while the panel is offline: there is nothing worth connecting
        to, and on a proxy an attempt costs a connection slot and a pause in
        scanning that every other device behind it pays for.

        Otherwise the panel is assumed to be keeping its duty cycle, so the next
        wake is one cycle on from the last sighting -- or several, if sightings
        have been missed, which is why this works off the phase rather than the
        raw age. The attempt opens LEAD seconds ahead of it.
        """
        age = self._last_seen()
        if age is None or age > ONLINE_TTL:
            return None
        if age <= INSIDE_WINDOW:
            # Awake right now, most likely, so go -- unless the last attempt
            # failed and nothing has happened since, because a push that fails
            # instantly would otherwise re-kick itself as fast as the event loop
            # allows until the sighting aged out.
            return MIN_DELAY if self._cooling_off else 0.0
        cycle = (
            bluetooth.async_get_learned_advertising_interval(self.hass, self.address)
            or CYCLE_FALLBACK
        )
        return max(MIN_DELAY, cycle - (age % cycle) - LEAD)

    @callback
    def _maybe_upload(self) -> None:
        if self.status != STATUS_PENDING:
            return
        if self._task is not None and not self._task.done():
            _LOGGER.debug("espaper %s: upload already in flight", self.address)
            return
        if (delay := self._next_attempt_delay()) is None or delay > 0:
            self._schedule_retry()
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
        """Arm the next attempt, aimed at the panel's next predicted wake."""
        self._cancel_retry()

        @callback
        def _retry(_now) -> None:
            self._retry_unsub = None
            # Whatever we were waiting out, we have now waited it out.
            self._cooling_off = False
            self._maybe_upload()

        delay = self._next_attempt_delay()
        _LOGGER.debug(
            "espaper %s: next attempt in %.0fs%s",
            self.address,
            OFFLINE_POLL if delay is None else delay,
            " (panel offline)" if delay is None else "",
        )
        if delay is None:
            delay = OFFLINE_POLL
        self._retry_unsub = async_call_later(self.hass, delay, _retry)

    @callback
    def _async_tick(self, _now) -> None:
        """Age the online flag out, and un-wedge a frame with nothing armed."""
        self._refresh_online()
        if self._retry_unsub is None:
            self._maybe_upload()

    async def _async_upload(self) -> None:
        """One upload attempt for whatever the text says right now."""
        sending, gray4, rotation = self.markdown, self.gray4, self.rotation
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
                device,
                lambda w, h, g: render_markdown(text, (w, h), g, rotation),
                gray4,
            )
        except Exception as err:  # noqa: BLE001
            # Expected while the board is in its ~60 s deep sleep: it is only
            # reachable for about two seconds per cycle. Deliberately every
            # exception, not a tuple: bleak raises BleakError, which descends
            # from Exception and not from OSError, and anything escaping this
            # background task strands the status at "uploading" with no retry
            # armed -- the panel then never updates again until a restart.
            _LOGGER.debug("espaper %s: upload failed: %s", self.address, err)
            # Not catching the panel is the normal case -- it is connectable for
            # about two seconds a minute -- so a connect that timed out is not
            # worth painting the card red over. An EPaperError is different: the
            # panel answered and the frame was refused. The offline sensor is
            # what says the panel is genuinely not there.
            self.last_error = str(err) if isinstance(err, EPaperError) else None
            self.status = STATUS_PENDING
            self._cooling_off = True
        else:
            self.uploaded_markdown = sending
            self.uploaded_gray4 = gray4
            self.uploaded_rotation = rotation
            self.last_error = None
            self.last_upload = dt_util.utcnow()
            # Anything changed mid-upload -- text or colour depth -- is still
            # owed to the panel; otherwise this is the latch that stops us
            # connecting on every wake.
            self.status = (
                STATUS_IDLE
                if self._current() == (sending, gray4, rotation)
                else STATUS_PENDING
            )
            _LOGGER.debug("espaper %s: upload confirmed by the panel", self.address)

        self._notify()
        # Clear first: _maybe_upload's in-flight guard would otherwise see this
        # very task, which is still running, and refuse to re-kick.
        self._task = None
        # Whether this failed or the text was edited mid-upload, the panel is
        # asleep now -- it sleeps the moment it has rendered -- so both want the
        # same thing: the next attempt aimed at the next wake.
        self._maybe_upload()

    @callback
    def _current(self) -> tuple[str, bool, int]:
        """Everything a frame depends on, for comparing against what was sent."""
        return self.markdown, self.gray4, self.rotation

    @callback
    def _notify(self) -> None:
        for listener in list(self._listeners):
            listener()
