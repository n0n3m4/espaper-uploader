#!/usr/bin/env python3
"""Self-checks for the Markdown renderer. No Home Assistant, no BLE adapter.

    python test_render.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "custom_components" / "espaper"))

from render import (  # noqa: E402
    GRAY_LEVELS,
    pack_1bpp,
    pack_2bpp,
    quantize_gray4,
    render_canvas,
    render_image,
    render_markdown,
    unpack_1bpp,
    unpack_2bpp,
)

SIZE = (400, 300)
EXPECTED = (SIZE[0] + 7) // 8 * SIZE[1]  # 15000 bytes
EXPECTED_GRAY4 = SIZE[0] // 4 * SIZE[1]  # 30000 bytes

SAMPLE = """# Shopping

Milk, **eggs**, and a *very* long line that has to wrap somewhere sensible.

- [x] bread
- [ ] 500 g of `flour`
  - the ~~cheap~~ ***good*** kind
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


def test_gray4_payload_geometry():
    payload = render_markdown(SAMPLE, SIZE, gray4=True)
    assert len(payload) == EXPECTED_GRAY4, len(payload)
    assert any(payload), "frame is entirely white"


def test_gray4_is_actually_antialiased():
    # The point of 4 colours: glyph edges keep the greys the renderer drew,
    # rather than being thresholded away. If someone reinstates a threshold on
    # this path, only two levels survive and this fails.
    levels = quantize_gray4(render_canvas(SAMPLE, SIZE))
    assert set(levels) == {0, 1, 2, 3}, sorted(set(levels))


def test_gray4_roundtrip():
    levels = quantize_gray4(render_canvas(SAMPLE, SIZE))
    assert max(levels) <= 3
    painted = unpack_2bpp(pack_2bpp(levels), SIZE)
    # Every pixel comes back as the luminance its level stands for. Not
    # re-quantised: quantize_gray4 reads canvas coverage, and `painted` is
    # already in panel luminance, so the two are not the same space.
    assert len(painted.tobytes()) == SIZE[0] * SIZE[1]
    assert painted.tobytes() == bytes(GRAY_LEVELS[v] for v in levels)


def test_rotation_fills_the_same_frame():
    # A quarter turn lays out at 300x400 and comes back as a 400x300 frame:
    # the wire format never changes, only what is drawn into it.
    for rotation in (0, 90, 180, 270):
        payload = render_markdown(SAMPLE, SIZE, rotation=rotation)
        assert len(payload) == EXPECTED, (rotation, len(payload))
        assert any(payload), f"{rotation} deg is entirely white"
    assert render_markdown(SAMPLE, SIZE, rotation=90) != render_markdown(SAMPLE, SIZE)
    assert len(render_markdown(SAMPLE, SIZE, gray4=True, rotation=270)) == EXPECTED_GRAY4


def test_rotation_turns_the_right_way():
    # 90 is clockwise, so the top-left of the upright layout -- where the
    # heading sits -- must end up in the top-right.
    def quadrant_ink(rotation, box):
        image = unpack_1bpp(render_markdown("# Hi", SIZE, rotation=rotation), SIZE)
        return sum(1 for v in image.crop(box).convert("L").tobytes() if v == 0)

    top_left, top_right = (0, 0, 200, 150), (200, 0, 400, 150)
    assert quadrant_ink(0, top_left) > quadrant_ink(0, top_right)
    assert quadrant_ink(90, top_right) > quadrant_ink(90, top_left)


def test_empty_is_blank():
    assert render_markdown("", SIZE) == b"\x00" * EXPECTED
    # Level 0 is white, so a blank 4-colour frame is zeroes too.
    assert render_markdown("", SIZE, gray4=True) == b"\x00" * EXPECTED_GRAY4


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


def test_inline_extensions():
    def ink(source):
        return sum(1 for v in render_image(source, SIZE).convert("L").tobytes() if v == 0)

    # A strike rule is ink the plain word does not have, and neither render may
    # be the one with the tildes still in it.
    assert ink("~~x~~") > ink("x")
    assert render_markdown("~~x~~", SIZE) != render_markdown("x", SIZE)
    assert render_markdown("~~x~~", SIZE) != render_markdown("\\~\\~x", SIZE)
    # Emphasis folds into bold, so three markers are just bold -- and, in
    # particular, leave no stray asterisk behind.
    assert render_markdown("***x***", SIZE) == render_markdown("**x**", SIZE)
    assert render_markdown("___x___", SIZE) == render_markdown("**x**", SIZE)
    # Strike survives emphasis inside it: bolder than the struck plain word.
    assert ink("~~**x**~~") > ink("~~x~~")


def test_list_extensions():
    # Bullet, empty box and ticked box are three different markers.
    frames = {
        render_markdown(source, SIZE)
        for source in ("- a", "- [ ] a", "- [x] a")
    }
    assert len(frames) == 3
    # ...and the marker is consumed, not drawn as literal brackets.
    assert render_markdown("- [ ] a", SIZE) != render_markdown("- \\[ \\] a", SIZE)
    # Nesting steps in, and stops stepping before it runs out of panel.
    assert render_markdown("- a\n  - b", SIZE) != render_markdown("- a\n- b", SIZE)
    assert render_markdown("- " + "x" * 40, SIZE)
    deep = "      - x"  # depth 3, the cap
    assert render_markdown(deep, SIZE) == render_markdown("        - x", SIZE)


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
