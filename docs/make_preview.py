#!/usr/bin/env python3
"""Regenerate docs/preview.png from docs/sample.md.

    python docs/make_preview.py

The panel is rendered at its native 400x300 and then doubled with nearest
neighbour, so the README image stays pixel-crisp on a high-DPI screen instead
of being smoothed by the browser. It goes through the wire format and back so
that what is shown is the quantised 4-colour frame -- the measured grey levels,
not the smooth canvas. It is set into a grey bezel because a mostly-white frame
on GitHub's dark theme reads as an unbounded white slab, and on the light theme
has no edge at all.
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "espaper"))

from render import render_markdown, unpack_2bpp  # noqa: E402

SCALE = 2
BEZEL = 18 * SCALE
RADIUS = 14 * SCALE
BEZEL_COLOUR = (58, 58, 60)


def main() -> None:
    here = Path(__file__).parent
    frame = render_markdown((here / "sample.md").read_text(), (400, 300), gray4=True)
    panel = unpack_2bpp(frame, (400, 300))
    panel = panel.resize(
        (panel.width * SCALE, panel.height * SCALE), Image.NEAREST
    )

    size = (panel.width + 2 * BEZEL, panel.height + 2 * BEZEL)
    out = Image.new("RGBA", size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(out)
    draw.rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), RADIUS, fill=BEZEL_COLOUR)
    out.paste(panel.convert("RGB"), (BEZEL, BEZEL))
    # Hairline between bezel and glass, so the white does not bleed into grey.
    draw.rectangle(
        (BEZEL - 1, BEZEL - 1, BEZEL + panel.width, BEZEL + panel.height),
        outline=(120, 120, 122),
    )

    out.save(here / "preview.png")
    print(f"wrote {here / 'preview.png'} at {size[0]}x{size[1]}")


if __name__ == "__main__":
    main()
