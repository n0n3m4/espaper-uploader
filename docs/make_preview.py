#!/usr/bin/env python3
"""Regenerate docs/preview.png from docs/sample.md.

    python docs/make_preview.py

The panel is rendered at its native 400x300 and then doubled with nearest
neighbour, so the README image stays pixel-crisp on a high-DPI screen instead
of being smoothed by the browser. It is set into a grey bezel because a bare
1bpp frame is pure white: on GitHub's dark theme that reads as an unbounded
white slab, and on the light theme it has no edge at all.
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "espaper"))

from render import render_image  # noqa: E402

SCALE = 2
BEZEL = 18 * SCALE
RADIUS = 14 * SCALE
BEZEL_COLOUR = (58, 58, 60)


def main() -> None:
    here = Path(__file__).parent
    panel = render_image((here / "sample.md").read_text(), (400, 300))
    panel = panel.convert("L").resize(
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
