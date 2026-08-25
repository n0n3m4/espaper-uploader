#!/usr/bin/env python3
"""Self-checks for the upload-once latch. Needs Home Assistant importable,
but no HA instance, no BLE adapter and no panel.

    python test_coordinator.py

The latch is the whole point of the integration: once the panel is showing the
current text, an advertisement must not cause another connection.
"""

import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))

from custom_components.espaper.coordinator import (  # noqa: E402
    BURST,
    MIN_DELAY,
    OFFLINE_POLL,
    ONLINE_TTL,
    STATUS_IDLE,
    STATUS_PENDING,
    EPaperCoordinator,
    expand_escapes,
)
from custom_components.espaper.render import render_markdown  # noqa: E402
from custom_components.espaper.epaper import EPaperError  # noqa: E402

ADDRESS = "AA:BB:CC:DD:EE:FF"


class _CapturedTimer:
    """Stands in for the unsub that async_call_later returns.

    Keeps the delay it was armed with, since when the next attempt is aimed is
    now the whole point.
    """

    def __init__(self, callback, delay=None):
        self.callback = callback
        self.delay = delay

    def __call__(self):  # cancelling the timer
        self.callback = lambda _now: None


class FakeDisplay:
    """Records every push, and can be told to fail."""

    def __init__(self):
        self.pushes: list[bytes] = []
        # Every net opened, failed ones included -- pushes only counts the ones
        # that landed.
        self.attempts = 0
        self.fail = False
        self.error: Exception = EPaperError("boom")

    async def push(self, device, payload_for, gray4=False):
        self.attempts += 1
        if self.fail:
            raise self.error
        self.pushes.append(payload_for(400, 300, gray4))


def build() -> tuple[EPaperCoordinator, FakeDisplay]:
    """A coordinator whose background tasks are real asyncio tasks.

    Retry timers are captured rather than scheduled, so a test can fire them
    on demand instead of waiting RETRY_DELAY seconds.
    """
    entry = MagicMock()
    tasks: list[asyncio.Task] = []
    entry.async_create_background_task = lambda hass, coro, name: tasks.append(
        task := asyncio.get_running_loop().create_task(coro)
    ) or task
    coordinator = EPaperCoordinator(MagicMock(), entry, ADDRESS)
    coordinator._tasks = tasks
    coordinator._timers = []
    display = FakeDisplay()
    coordinator.display = display
    coordinator._device = MagicMock()  # pretend the panel is already known
    return coordinator, display


def service_info(age: float = 0.0):
    """A sighting of the panel `age` seconds ago, as HA's history reports it."""
    info = MagicMock()
    info.time = time.monotonic() - age
    return info


def seen(age: float):
    """Patch the advertisement history to a sighting `age` seconds old."""
    return patch(
        "custom_components.espaper.coordinator.bluetooth.async_last_service_info",
        return_value=service_info(age),
    )


def armed_delay(coordinator) -> int:
    """How long the armed attempt was given, to the nearest second.

    The ages these tests set are real elapsed time, so the delays come out a
    hair under the arithmetic; nothing here turns on a fraction of a second.
    """
    assert coordinator._retry_unsub is not None, "no attempt armed"
    return round(coordinator._retry_unsub.delay)


def fire_retry(coordinator) -> bool:
    """Run the pending retry timer, if one is armed. Returns whether it was."""
    unsub = coordinator._retry_unsub
    if unsub is None:
        return False
    coordinator._retry_unsub = None
    unsub.callback(None)
    return True


async def settle(coordinator):
    """Let every queued upload task run to completion."""
    for _ in range(10):
        await asyncio.sleep(0)
        pending = [t for t in coordinator._tasks if not t.done()]
        if not pending:
            break
        await asyncio.gather(*pending)


def advertise(coordinator):
    """Simulate the panel waking up and advertising."""
    info = MagicMock()
    coordinator._async_on_advertisement(info, MagicMock())


async def test_uploads_once_then_latches():
    coordinator, display = build()
    await coordinator.async_set_markdown("# Hello")
    await settle(coordinator)
    assert len(display.pushes) == 1, display.pushes
    assert coordinator.status == STATUS_IDLE

    # Ten more wake-ups must not touch the radio again.
    for _ in range(10):
        advertise(coordinator)
        await settle(coordinator)
    assert len(display.pushes) == 1, "latch leaked: uploaded again while idle"


async def test_new_text_uploads_again():
    coordinator, display = build()
    await coordinator.async_set_markdown("one")
    await settle(coordinator)
    await coordinator.async_set_markdown("two")
    await settle(coordinator)
    assert len(display.pushes) == 2
    assert display.pushes[0] != display.pushes[1]
    assert coordinator.uploaded_markdown == "two"


async def test_same_text_is_a_noop():
    coordinator, display = build()
    await coordinator.async_set_markdown("same")
    await settle(coordinator)
    await coordinator.async_set_markdown("same")
    await settle(coordinator)
    assert len(display.pushes) == 1


async def test_failure_retries_on_next_advertisement():
    coordinator, display = build()
    display.fail = True
    await coordinator.async_set_markdown("# Hello")
    await settle(coordinator)
    assert coordinator.status == STATUS_PENDING, "a failure must stay pending"
    assert coordinator.last_error

    display.fail = False
    advertise(coordinator)
    await settle(coordinator)
    assert len(display.pushes) == 1
    assert coordinator.status == STATUS_IDLE
    assert coordinator.last_error is None


async def test_edit_during_upload_is_not_lost():
    coordinator, display = build()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_push(device, payload_for, gray4=False):
        started.set()
        await release.wait()
        display.pushes.append(payload_for(400, 300, gray4))

    coordinator.display.push = slow_push
    await coordinator.async_set_markdown("first")
    await started.wait()
    # Second edit lands mid-flight: it must not be dropped on the floor.
    await coordinator.async_set_markdown("second")
    release.set()
    coordinator.display.push = FakeDisplay.push.__get__(display)
    await settle(coordinator)
    assert coordinator.uploaded_markdown == "second"
    assert coordinator.status == STATUS_IDLE


async def test_restore_after_restart_does_not_reupload():
    # E-paper holds its image without power, so if the panel already had this
    # text before the restart there is nothing to send.
    coordinator, display = build()
    coordinator.async_restore("# Hello", "# Hello")
    await settle(coordinator)
    assert coordinator.status == STATUS_IDLE
    assert not display.pushes

    # ...but an unfinished upload must resume.
    coordinator, display = build()
    coordinator.async_restore("# New", "# Old")
    await settle(coordinator)
    assert len(display.pushes) == 1
    assert coordinator.status == STATUS_IDLE


async def test_colour_switch_repaints():
    # Toggling the depth must send the same text again, in the other depth:
    # the panel is holding a frame that no longer matches the settings.
    coordinator, display = build()
    await coordinator.async_set_markdown("# Hello")
    await settle(coordinator)
    assert len(display.pushes[0]) == 30000, "4 colours is the default"

    await coordinator.async_set_gray4(False)
    await settle(coordinator)
    assert len(display.pushes) == 2
    assert len(display.pushes[1]) == 15000
    assert coordinator.status == STATUS_IDLE

    # ...and setting it to what it already is changes nothing.
    await coordinator.async_set_gray4(False)
    await settle(coordinator)
    assert len(display.pushes) == 2


async def test_colour_switch_during_upload_is_not_lost():
    # The mirror of test_edit_during_upload_is_not_lost: the latch has to key
    # on the depth as well as the text, or a mid-upload toggle is swallowed.
    coordinator, display = build()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_push(device, payload_for, gray4=False):
        started.set()
        await release.wait()
        display.pushes.append(payload_for(400, 300, gray4))

    coordinator.display.push = slow_push
    await coordinator.async_set_markdown("# Hello")
    await started.wait()
    await coordinator.async_set_gray4(False)
    release.set()
    coordinator.display.push = FakeDisplay.push.__get__(display)
    await settle(coordinator)
    assert len(display.pushes) == 2, "the toggle was dropped"
    assert len(display.pushes[-1]) == 15000
    assert coordinator.status == STATUS_IDLE


async def test_restored_colour_does_not_reupload():
    # E-paper holds its image, so a restart must not repaint a panel that is
    # already showing the right frame in the right depth.
    coordinator, display = build()
    coordinator.async_restore_gray4(False)
    coordinator.async_restore("# Hello", "# Hello")
    await settle(coordinator)
    assert coordinator.status == STATUS_IDLE
    assert not display.pushes
    assert coordinator.gray4 is False


async def test_rotation_repaints():
    coordinator, display = build()
    await coordinator.async_set_markdown("# Hello")
    await settle(coordinator)

    await coordinator.async_set_rotation(90)
    await settle(coordinator)
    assert len(display.pushes) == 2
    assert display.pushes[1] == render_markdown(
        "# Hello", (400, 300), gray4=True, rotation=90
    )
    assert coordinator.status == STATUS_IDLE

    # ...and setting it to what it already is changes nothing.
    await coordinator.async_set_rotation(90)
    await settle(coordinator)
    assert len(display.pushes) == 2


async def test_restored_rotation_does_not_reupload():
    coordinator, display = build()
    coordinator.async_restore("# Hello", "# Hello", 180, 180)
    await settle(coordinator)
    assert coordinator.status == STATUS_IDLE
    assert not display.pushes
    assert coordinator.rotation == 180

    # ...but a rotation that never reached the panel must still be sent.
    coordinator, display = build()
    coordinator.async_restore("# Hello", "# Hello", 90, 0)
    await settle(coordinator)
    assert len(display.pushes) == 1
    assert display.pushes[0] == render_markdown(
        "# Hello", (400, 300), gray4=True, rotation=90
    )


async def test_escaped_newlines_reach_the_panel():
    # The single-line text box cannot hold a real newline, so a typed "\n"
    # has to render as one -- otherwise every heading and bullet is impossible
    # to enter from the UI.
    coordinator, display = build()
    await coordinator.async_set_markdown("# Title\\n\\n- one\\n- two")
    await settle(coordinator)
    assert display.pushes[0] == render_markdown(
        "# Title\n\n- one\n- two", (400, 300), gray4=True
    )
    # ...but the entity keeps showing what was typed, so editing round-trips.
    assert coordinator.markdown == "# Title\\n\\n- one\\n- two"


def test_real_newlines_are_untouched():
    body = "# Title\n\n- one"
    assert expand_escapes(body) == body


async def test_retries_without_any_advertisement():
    """The bug this guards: Home Assistant suppresses repeat advertisements.

    HA drops an advertisement identical to the previous one, and this board
    broadcasts a constant payload, so after the first sighting the callback
    stops arriving. Delivery therefore cannot depend on it -- a failed upload
    must retry on a timer with no further advertisements at all.
    """
    coordinator, display = build()
    display.fail = True
    await coordinator.async_set_markdown("# Hello")
    await settle(coordinator)
    assert coordinator.status == STATUS_PENDING
    assert coordinator._retry_unsub is not None, "no retry armed after a failure"

    # The panel wakes, but HA never tells us. Only the timer can save this.
    display.fail = False
    assert fire_retry(coordinator)
    await settle(coordinator)
    assert len(display.pushes) == 1, "timer retry did not deliver the frame"
    assert coordinator.status == STATUS_IDLE


async def test_keeps_the_net_open_while_online():
    """No prediction: while a frame is owed, just keep re-arming the net.

    A connect attempt is a 20 s net -- the controller holds the request pending
    and latches onto the panel's first advertisement -- so back-to-back
    attempts leave nowhere for the panel to wake up unnoticed. Aiming one net
    at a predicted wake was fragile: every miss cost a whole cycle.
    """
    coordinator, display = build()
    display.fail = True
    with seen(20):
        # Mid-sleep is not a reason to wait: the net is open for 20 s and the
        # panel walks into it when it wakes.
        await coordinator.async_set_markdown("# Hello")
        await settle(coordinator)
        assert display.attempts == 1, "waited out the sleep instead of netting"
        assert coordinator.status == STATUS_PENDING
        assert armed_delay(coordinator) == MIN_DELAY, "no cool-off after a failure"

        # The cool-off delays one net; it must not become the answer forever.
        # The bug it guards: the timer fires, re-decides the same MIN_DELAY and
        # arms itself again, so a burst spins at 2 s and never connects at all.
        display.fail = False
        assert fire_retry(coordinator)
        await settle(coordinator)
        assert display.attempts == 2, "the cool-off swallowed the whole burst"
    assert len(display.pushes) == 1
    assert coordinator.status == STATUS_IDLE


async def test_burst_expires_into_a_quiet_gap():
    """The burst is bounded, because our own connecting blinds the scanner.

    On an ESPHome proxy a pending connect takes a connection slot and stops the
    scanning -- including the advertisements that are the only evidence the
    panel is still there. So the nets stop after BURST and the radio is left
    alone for a while.
    """
    coordinator, display = build()
    display.fail = True
    with seen(20):
        await coordinator.async_set_markdown("# Hello")
        await settle(coordinator)
        assert armed_delay(coordinator) == MIN_DELAY

        coordinator._burst_until = time.monotonic() - 1
        display.attempts = 0
        assert fire_retry(coordinator)
        await settle(coordinator)
    assert not display.attempts, "kept hammering past the end of the burst"
    assert armed_delay(coordinator) == OFFLINE_POLL


async def test_a_fresh_sighting_rearms_the_burst():
    """A panel that is still cycling is worth another burst.

    Not driven by the advertisement callback: HA suppresses this board's repeat
    advertisements, so the tick reading the history is what has to notice.
    """
    coordinator, display = build()
    display.fail = True
    with seen(20):
        await coordinator.async_set_markdown("# Hello")
        await settle(coordinator)
        coordinator._burst_until = time.monotonic() - 1
        assert fire_retry(coordinator)
        await settle(coordinator)
        assert armed_delay(coordinator) == OFFLINE_POLL

    # The quiet gap lets the proxy scan again, and it hears the panel.
    with seen(10):
        display.attempts = 0
        coordinator._async_tick(None)
        await settle(coordinator)
    assert display.attempts == 1, "a live panel was left queued"
    assert coordinator._burst_until - time.monotonic() > BURST - 5


async def test_offline_panel_is_left_alone():
    """Past ONLINE_TTL the panel is off, flat or out of range.

    Attempts stop entirely: on a proxy each one takes a connection slot and
    pauses the scanning every other device behind it depends on. Only the
    advertisement history is watched, and that costs a dict lookup.
    """
    coordinator, display = build()
    with seen(ONLINE_TTL + 60):
        await coordinator.async_set_markdown("# Hello")
        await settle(coordinator)
    assert not display.pushes, "connected to a panel that is not there"
    assert armed_delay(coordinator) == OFFLINE_POLL

    # A panel HA has no sighting of at all is the same case: it drops a device
    # from its history once it has been quiet long enough.
    with patch(
        "custom_components.espaper.coordinator.bluetooth.async_last_service_info",
        return_value=None,
    ):
        assert fire_retry(coordinator)
        await settle(coordinator)
    assert not display.pushes, "connected to a panel HA has never seen"
    assert armed_delay(coordinator) == OFFLINE_POLL

    # ...and the frame is still owed when it comes back.
    assert fire_retry(coordinator)
    await settle(coordinator)
    assert len(display.pushes) == 1


async def test_online_flag_ages_out_on_the_tick():
    # Going offline is the passage of time, not an event, and HA suppresses
    # this board's repeat advertisements -- so only the tick can notice.
    coordinator, _ = build()
    updates = []
    coordinator.async_add_listener(lambda: updates.append(coordinator.online))

    coordinator._async_tick(None)
    assert coordinator.online is True
    assert coordinator.last_seen is not None
    assert updates == [True], "entities were not told the panel appeared"

    with seen(ONLINE_TTL + 1):
        coordinator._async_tick(None)
    assert coordinator.online is False
    assert updates == [True, False]

    # Steady state notifies nobody: entity writes are not free.
    coordinator._async_tick(None)
    coordinator._async_tick(None)
    assert updates == [True, False, True]


async def test_a_missed_wake_is_not_an_error():
    # The card shows last_error in red. A connect that never landed is what
    # happens most cycles, and saying so there reads like a fault; only the
    # panel answering and refusing the frame is one.
    coordinator, display = build()
    display.fail = True
    display.error = TimeoutError("Timeout waiting for connect response after 20s")
    await coordinator.async_set_markdown("# Hello")
    await settle(coordinator)
    assert coordinator.status == STATUS_PENDING
    assert coordinator.last_error is None, "a missed wake was reported as a fault"

    display.error = EPaperError("state=ERROR err=CRC_MISMATCH received=15000")
    assert fire_retry(coordinator)
    await settle(coordinator)
    assert coordinator.last_error == "state=ERROR err=CRC_MISMATCH received=15000"


async def test_failure_does_not_spin():
    # A push that fails instantly, against a sighting fresh enough to mean
    # "awake now", would otherwise re-kick itself as fast as the event loop
    # allows until the sighting aged out.
    coordinator, display = build()
    display.fail = True
    await coordinator.async_set_markdown("# Hello")
    await settle(coordinator)
    assert armed_delay(coordinator) == MIN_DELAY, "no cool-off after a failure"

    # ...but an advertisement is proof the panel is up this instant, and a spin
    # does not advertise, so that outranks the cool-off.
    display.fail = False
    advertise(coordinator)
    await settle(coordinator)
    assert len(display.pushes) == 1


async def test_unexpected_error_does_not_strand_the_status():
    """bleak raises BleakError, which is not an OSError.

    An exception escaping the upload task leaves the status at "uploading"
    with no retry armed, and the panel never updates again until Home
    Assistant restarts -- exactly what the card was showing.
    """
    coordinator, display = build()
    display.fail = True
    display.error = RuntimeError("le-connection-abort-by-local")
    await coordinator.async_set_markdown("# Hello")
    await settle(coordinator)
    assert coordinator.status == STATUS_PENDING, coordinator.status
    assert coordinator._retry_unsub is not None, "no retry armed after a failure"

    display.fail = False
    assert fire_retry(coordinator)
    await settle(coordinator)
    assert len(display.pushes) == 1


async def test_success_arms_no_retry():
    coordinator, display = build()
    await coordinator.async_set_markdown("# Hello")
    await settle(coordinator)
    assert coordinator.status == STATUS_IDLE
    assert coordinator._retry_unsub is None, "idle must not keep a timer armed"


async def test_upload_without_a_cached_device_still_retries():
    coordinator, display = build()
    coordinator._device = None
    with patch(
        "custom_components.espaper.coordinator.bluetooth"
        ".async_ble_device_from_address",
        return_value=None,
    ):
        await coordinator.async_set_markdown("# Hello")
        await settle(coordinator)
    assert not display.pushes
    assert coordinator.status == STATUS_PENDING
    # ...on a timer, not immediately: re-entering here with no device is a hot
    # loop, so it gets the same cool-off a failure does.
    assert armed_delay(coordinator) == MIN_DELAY


async def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            result = fn()
            if asyncio.iscoroutine(result):
                await result
            print(f"ok  {name}")
    print("all good")


if __name__ == "__main__":
    # Capture retry timers instead of really scheduling them, and keep the
    # bluetooth lookup from needing a running Home Assistant.
    with (
        patch(
            "custom_components.espaper.coordinator.async_call_later",
            new=lambda hass, delay, action: _CapturedTimer(action, delay),
        ),
        patch(
            "custom_components.espaper.coordinator.bluetooth"
            ".async_ble_device_from_address",
            return_value=None,
        ),
        # Awake unless a test says otherwise.
        patch(
            "custom_components.espaper.coordinator.bluetooth"
            ".async_last_service_info",
            new=lambda *args, **kwargs: service_info(),
        ),
    ):
        asyncio.run(main())
