#!/usr/bin/env python3
"""Self-checks for the upload-once latch. Needs Home Assistant importable,
but no HA instance, no BLE adapter and no panel.

    python test_coordinator.py

The latch is the whole point of the integration: once the panel is showing the
current text, an advertisement must not cause another connection.
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent))

from custom_components.espaper.coordinator import (  # noqa: E402
    STATUS_IDLE,
    STATUS_PENDING,
    EPaperCoordinator,
    expand_escapes,
)
from custom_components.espaper.render import render_markdown  # noqa: E402
from custom_components.espaper.epaper import EPaperError  # noqa: E402

ADDRESS = "AA:BB:CC:DD:EE:FF"


class _CapturedTimer:
    """Stands in for the unsub that async_call_later returns."""

    def __init__(self, callback):
        self.callback = callback

    def __call__(self):  # cancelling the timer
        self.callback = lambda _now: None


class FakeDisplay:
    """Records every push, and can be told to fail."""

    def __init__(self):
        self.pushes: list[bytes] = []
        self.fail = False

    async def push(self, device, payload_for, gray4=False):
        if self.fail:
            raise EPaperError("boom")
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
    assert coordinator._retry_unsub is not None


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
            new=lambda hass, delay, action: _CapturedTimer(action),
        ),
        patch(
            "custom_components.espaper.coordinator.bluetooth"
            ".async_ble_device_from_address",
            return_value=None,
        ),
    ):
        asyncio.run(main())
