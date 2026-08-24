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
from unittest.mock import MagicMock

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


class FakeDisplay:
    """Records every push, and can be told to fail."""

    def __init__(self):
        self.pushes: list[bytes] = []
        self.fail = False

    async def push(self, device, payload_for):
        if self.fail:
            raise EPaperError("boom")
        self.pushes.append(payload_for(400, 300))


def build() -> tuple[EPaperCoordinator, FakeDisplay]:
    """A coordinator whose background tasks are real asyncio tasks."""
    entry = MagicMock()
    tasks: list[asyncio.Task] = []
    entry.async_create_background_task = lambda hass, coro, name: tasks.append(
        task := asyncio.get_running_loop().create_task(coro)
    ) or task
    coordinator = EPaperCoordinator(MagicMock(), entry, ADDRESS)
    coordinator._tasks = tasks
    display = FakeDisplay()
    coordinator.display = display
    coordinator._device = MagicMock()  # pretend the panel is already known
    return coordinator, display


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

    async def slow_push(device, payload_for):
        started.set()
        await release.wait()
        display.pushes.append(payload_for(400, 300))

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


async def test_escaped_newlines_reach_the_panel():
    # The single-line text box cannot hold a real newline, so a typed "\n"
    # has to render as one -- otherwise every heading and bullet is impossible
    # to enter from the UI.
    coordinator, display = build()
    await coordinator.async_set_markdown("# Title\\n\\n- one\\n- two")
    await settle(coordinator)
    assert display.pushes[0] == render_markdown("# Title\n\n- one\n- two", (400, 300))
    # ...but the entity keeps showing what was typed, so editing round-trips.
    assert coordinator.markdown == "# Title\\n\\n- one\\n- two"


def test_real_newlines_are_untouched():
    body = "# Title\n\n- one"
    assert expand_escapes(body) == body


async def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            result = fn()
            if asyncio.iscoroutine(result):
                await result
            print(f"ok  {name}")
    print("all good")


if __name__ == "__main__":
    asyncio.run(main())
