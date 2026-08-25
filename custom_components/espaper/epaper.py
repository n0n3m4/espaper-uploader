"""BLE protocol driver for the ESPaper display.

No Home Assistant imports, so it can be exercised standalone against a real
board:

    python epaper.py notes.md            # 4 colours, as the integration sends
    python epaper.py notes.md --mono     # black and white

Ported from ``tools/epaper_push.py`` in the firmware repo. The wire protocol,
the status codes and the chunking are unchanged; what is different is the
connection: inside Home Assistant the bluetooth stack supplies the
``BLEDevice`` and ``bleak_retry_connector`` owns the retry policy, so the
CLI's own scan-and-hammer loop is gone.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import zlib
from collections.abc import Callable

from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

_LOGGER = logging.getLogger(__name__)

# Vendor service, matching the firmware's src/config.h.
SVC_UUID = "69770000-b271-453a-a64d-cb31cec14bee"
CHR_CTRL = "69770001-b271-453a-a64d-cb31cec14bee"
CHR_DATA = "69770002-b271-453a-a64d-cb31cec14bee"
CHR_STATUS = "69770003-b271-453a-a64d-cb31cec14bee"
CHR_INFO = "69770004-b271-453a-a64d-cb31cec14bee"

PROTOCOL_VERSION = 1
MAGIC = b"EPD1"
OP_BEGIN, OP_COMMIT, OP_ABORT, OP_CLEAR = 1, 2, 3, 4

STATE_NAMES = {
    0: "IDLE",
    1: "RECEIVING",
    2: "VERIFYING",
    3: "RENDERING",
    4: "DONE",
    5: "ERROR",
}
ERROR_NAMES = {
    0: "NONE",
    1: "BAD_MAGIC",
    2: "BAD_VERSION",
    3: "BAD_GEOMETRY",
    4: "BAD_LENGTH",
    5: "CRC_MISMATCH",
    6: "OVERFLOW",
    7: "NO_SESSION",
    8: "NO_MEMORY",
    9: "INCOMPLETE",
    10: "INFLATE",
}
STATE_DONE, STATE_ERROR = 4, 5

# BEGIN flags. Bit 0 would invert the bit convention, which this client never
# does; bit 1 selects 2bpp (4 grey levels, 30000 bytes instead of 15000) and
# bit 2 says the payload is a zlib stream. Both are decided per frame.
FLAG_GRAY4 = 1 << 1
FLAG_DEFLATE = 1 << 2

# Largest ATT payload NimBLE will accept (MTU 256 minus the 3-byte write
# header), and the 4-byte offset each data chunk carries.
MAX_ATT_PAYLOAD = 253
DATA_HEADER_LEN = 4

# A full panel refresh takes several seconds; the firmware's own session
# watchdog is 60 s, so there is no point waiting longer than that.
RENDER_TIMEOUT = 60.0


class EPaperError(Exception):
    """The device rejected the frame, or never confirmed it."""


class _StatusTracker:
    """Latest STATUS notification, plus an event for terminal states."""

    def __init__(self) -> None:
        self.state: int | None = None
        self.error = 0
        self.received = 0
        self.terminal = asyncio.Event()
        self.updated = asyncio.Event()

    def handle(self, _sender: object, data: bytearray) -> None:
        if len(data) < 6:
            return
        self.state, self.error, self.received = struct.unpack("<BBI", data[:6])
        self.updated.set()
        if self.state in (STATE_DONE, STATE_ERROR):
            self.terminal.set()

    async def wait_update(self, timeout: float = 2.0) -> None:
        """Give an in-flight notification a chance to land before acting on it."""
        try:
            await asyncio.wait_for(self.updated.wait(), timeout=timeout)
        except TimeoutError:
            pass

    def describe(self) -> str:
        return (
            f"state={STATE_NAMES.get(self.state, self.state)} "
            f"err={ERROR_NAMES.get(self.error, self.error)} "
            f"received={self.received}"
        )


def _build_begin(
    width: int, height: int, length: int, crc: int, gray4: bool, deflate: bool
) -> bytes:
    return struct.pack(
        "<B4sBBHHII",
        OP_BEGIN,
        MAGIC,
        PROTOCOL_VERSION,
        (FLAG_GRAY4 if gray4 else 0) | (FLAG_DEFLATE if deflate else 0),
        width,
        height,
        length,
        crc,
    )


def _chunk_size(client: BleakClientWithServiceCache) -> int:
    """Bytes of image data per write, derived from the negotiated MTU."""
    # Not every backend exposes the MTU; fall back to the 23-byte BLE default,
    # which is slow but always works.
    try:
        mtu = client.mtu_size or 23
    except Exception:  # noqa: BLE001
        mtu = 23
    return max(16, min(mtu - 3, MAX_ATT_PAYLOAD) - DATA_HEADER_LEN)


class EPaperDisplay:
    """One-shot frame pusher for a single panel."""

    def __init__(self, address: str) -> None:
        self.address = address

    async def push(
        self,
        device: BLEDevice,
        payload_for: Callable[[int, int, bool], bytes],
        gray4: bool = False,
    ) -> None:
        """Connect, render to the panel's real geometry, upload, and commit.

        ``payload_for(width, height, gray4)`` is only called once INFO has been
        read, so the firmware -- not this client -- stays the authority on both
        panel size and colour depth: a board that advertises 1 bpp gets a 1bpp
        frame however this was called. Raises :class:`EPaperError` unless the
        device reports DONE.
        """
        # One attempt only, because the coordinator drives the loop: this is a
        # 20 s net -- the request stays pending and the controller latches onto
        # the panel's first advertisement -- and the coordinator opens the next
        # one the moment this returns. establish_connection's own retries reuse
        # one client, so a stale device path stays stale; the coordinator
        # re-resolves the BLEDevice for every attempt, which is the point.
        #
        # The 20 s is not ours to pick, and a longer net is not a one-line
        # change: establish_connection hardcodes client.connect(timeout=
        # BLEAK_TIMEOUT) at 20 s (kwargs here reach the client *constructor*,
        # not that call), and ESPHome's esp32_ble has connection_timeout,
        # default 20 s, chosen to match aioesphomeapi and bleak-retry-connector.
        # Both ends would have to change, including every proxy's YAML.
        client = await establish_connection(
            BleakClientWithServiceCache, device, self.address, max_attempts=1
        )
        try:
            width, height, bpp, sleep_s = await self._read_info(client)
            _LOGGER.debug(
                "espaper %s: panel %dx%d, %d bpp, sleeps %ds between adverts",
                self.address,
                width,
                height,
                bpp,
                sleep_s,
            )
            if gray4 and bpp < 2:
                _LOGGER.info(
                    "espaper %s: panel advertises 1 bpp, sending black and white",
                    self.address,
                )
                gray4 = False

            payload = payload_for(width, height, gray4)
            expected = width // 4 * height if gray4 else (width + 7) // 8 * height
            if len(payload) != expected:
                raise EPaperError(
                    f"rendered {len(payload)} bytes, panel wants {expected}"
                )

            status = _StatusTracker()
            await client.start_notify(CHR_STATUS, status.handle)
            await self._send(client, status, width, height, payload, gray4)

            try:
                await asyncio.wait_for(status.terminal.wait(), RENDER_TIMEOUT)
            except TimeoutError:
                raise EPaperError(f"timed out rendering ({status.describe()})") from None
            if status.state != STATE_DONE:
                raise EPaperError(status.describe())
            _LOGGER.debug("espaper %s: %s", self.address, status.describe())
        finally:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                # The board drops the link itself once it starts sleeping.
                _LOGGER.debug("espaper %s: error on disconnect", self.address)

    async def _read_info(
        self, client: BleakClientWithServiceCache
    ) -> tuple[int, int, int, int]:
        raw = await client.read_gatt_char(CHR_INFO)
        if len(raw) < 8:
            raise EPaperError(f"INFO characteristic too short: {len(raw)} bytes")
        version, width, height, bpp, sleep_s = struct.unpack("<BHHBH", raw[:8])
        if version != PROTOCOL_VERSION:
            raise EPaperError(
                f"device speaks protocol v{version}, this client speaks "
                f"v{PROTOCOL_VERSION}"
            )
        if bpp not in (1, 2):
            raise EPaperError(f"device reports {bpp} bpp, which this client cannot pack")
        return width, height, bpp, sleep_s

    async def _send(
        self,
        client: BleakClientWithServiceCache,
        status: _StatusTracker,
        width: int,
        height: int,
        payload: bytes,
        gray4: bool = False,
    ) -> None:
        # A page of rendered text deflates to roughly a sixth of a frame, which
        # is a sixth of the chunks and of the awake burst that dominates the
        # board's power budget. Nothing is lost when it does not compress: send
        # whichever is shorter and let the flag say which it was.
        raw = len(payload)
        squeezed = zlib.compress(payload, 9)
        deflate = len(squeezed) < raw
        if deflate:
            payload = squeezed

        crc = zlib.crc32(payload) & 0xFFFFFFFF
        chunk = _chunk_size(client)
        _LOGGER.debug(
            "espaper %s: sending %d of %d bytes (%d colours, deflate=%s), "
            "crc32=0x%08x, chunk=%d",
            self.address,
            len(payload),
            raw,
            4 if gray4 else 2,
            deflate,
            crc,
            chunk,
        )

        status.updated.clear()
        await client.write_gatt_char(
            CHR_CTRL,
            _build_begin(width, height, len(payload), crc, gray4, deflate),
            response=True,
        )
        # The device acks the header with a status notification; if it rejected
        # the header there is no point streaming 15 kB at it.
        await status.wait_update()
        if status.state == STATE_ERROR:
            raise EPaperError(f"header rejected ({status.describe()})")

        for offset in range(0, len(payload), chunk):
            await client.write_gatt_char(
                CHR_DATA,
                struct.pack("<I", offset) + payload[offset : offset + chunk],
                response=True,
            )
            if status.state == STATE_ERROR:
                raise EPaperError(f"upload rejected ({status.describe()})")

        status.terminal.clear()
        await client.write_gatt_char(CHR_CTRL, bytes([OP_COMMIT]), response=True)


if __name__ == "__main__":
    import sys
    from pathlib import Path

    from bleak import BleakScanner
    from render import render_markdown

    logging.basicConfig(level=logging.DEBUG)

    async def _main() -> int:
        argv = sys.argv[1:]
        gray4 = "--mono" not in argv
        rotation = 0
        if "--rotate" in argv:
            i = argv.index("--rotate")
            rotation = int(argv[i + 1])
            del argv[i : i + 2]
        args = [a for a in argv if a != "--mono"]
        text = Path(args[0]).read_text() if args else "# Hello"
        # The board advertises for ~2 s per minute, so scan for a full cycle.
        device = await BleakScanner.find_device_by_filter(
            lambda _d, adv: SVC_UUID.lower() in [u.lower() for u in adv.service_uuids],
            timeout=90.0,
        )
        if device is None:
            print("device not found", file=sys.stderr)
            return 1
        await EPaperDisplay(device.address).push(
            device,
            lambda w, h, g: render_markdown(text, (w, h), g, rotation),
            gray4,
        )
        return 0

    sys.exit(asyncio.run(_main()))
