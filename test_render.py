#!/usr/bin/env python3
"""Self-checks for the Markdown renderer. No Home Assistant, no BLE adapter.

    python test_render.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "custom_components" / "espaper"))

from render import pack_1bpp, render_image, render_markdown, unpack_1bpp  # noqa: E402

SIZE = (400, 300)
EXPECTED = (SIZE[0] + 7) // 8 * SIZE[1]  # 15000 bytes

SAMPLE = """# Shopping

Milk, **eggs**, and a *very* long line that has to wrap somewhere sensible.

- bread
- 500 g of `flour`
1. first
2. second

---

> quoted text
"""


def test_payload_geometry():
    payload = render_markdown(SAMPLE, SIZE)
    assert len(payload) == EXPECTED, len(payload)
    assert any(payload), "frame is entirely white"
    assert not all(b == 0xFF for b in payload), "frame is entirely black"


def test_roundtrip():
    image = render_image(SAMPLE, SIZE)
    assert unpack_1bpp(pack_1bpp(image), SIZE).tobytes() == image.tobytes()


def test_empty_is_blank():
    assert render_markdown("", SIZE) == b"\x00" * EXPECTED


def test_overflow_is_clipped():
    # 5 kB of text must still produce exactly one frame, not an exception.
    payload = render_markdown("word " * 1000, SIZE)
    assert len(payload) == EXPECTED


def test_unbreakable_word_fits():
    # A long URL must not spill past the right edge into an infinite loop.
    payload = render_markdown("x" * 500, SIZE)
    assert len(payload) == EXPECTED


def test_markup_is_not_literal():
    # The markers themselves must never reach the panel.
    plain = render_markdown("bold", SIZE)
    assert render_markdown("**bold**", SIZE) != plain  # bolder, so different
    # ...but a heading marker is consumed, not drawn:
    assert render_markdown("# Hi", SIZE) != render_markdown("\\# Hi", SIZE)


def test_nothing_runs_off_the_edge():
    # A pasted URL must be hard-split, not drawn past the right margin. The
    # case that bites is a long token arriving on an almost-full line, so the
    # padding is swept rather than guessed.
    sources = ["x" * 500, SAMPLE]
    sources += ["a" * pad + " " + "x" * 200 for pad in range(40, 56)]
    for source in sources:
        image = render_image(source, SIZE)
        columns = image.convert("L").tobytes()
        for row in range(SIZE[1]):
            line = columns[row * SIZE[0] : (row + 1) * SIZE[0]]
            rightmost = max(
                (i for i, v in enumerate(line) if v == 0), default=-1
            )
            assert rightmost < SIZE[0] - 2, f"ink at x={rightmost} on row {row}"


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"ok  {name}")
    print("all good")
