"""Render a small Markdown subset to a 1bpp e-paper frame.

No Home Assistant imports: run it directly to preview a layout.

    python render.py notes.md preview.png

Only Pillow is needed. A full Markdown library would hand back an AST and
leave the entire layout pass still to write, so the subset is parsed inline.

Everything here is tuned for a 1bpp panel rather than a screen:

* the bundled face is Noto Sans SemiCondensed **Medium** -- semicondensed to
  fit more characters per line, Medium because Regular's thin stems drop out
  when antialiased greys get thresholded (Inkycal defaults to it for the same
  reason),
* text is drawn antialiased on an "L" canvas and thresholded at `INK_CUTOFF`,
  which is set above mid-grey deliberately: edge pixels that are only slightly
  grey become ink, thickening stems instead of eroding them,
* `code` spans render as white-on-black boxes rather than in a mono face --
  at this size a weight change is invisible but an inverted box is not.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_DIR = Path(__file__).parent / "fonts"
FONT_REGULAR = FONT_DIR / "NotoSans-SemiCondensedMedium.ttf"
FONT_BOLD = FONT_DIR / "NotoSans-SemiCondensedSemiBold.ttf"

# Grey level at or above which a pixel stays white. Above 128 on purpose: it
# biases antialiased glyph edges towards ink so small text stays solid.
INK_CUTOFF = 160

BODY_SIZE = 15
HEADING_SIZES = {1: 26, 2: 21, 3: 17}
MARGIN = 8
# Leading added below each line, as a fraction of the font size. Noto's own
# ascent+descent carries far more slack than a 300 px panel can afford, so the
# pitch is built from the ink band instead (see _metrics).
LINE_SPACING = 0.3
PARAGRAPH_GAP = 5
INDENT = 14

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_BULLET = re.compile(r"^\s*[-*+]\s+(.*)$")
_ORDERED = re.compile(r"^\s*(\d+)[.)]\s+(.*)$")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
# Inline runs: **bold**, *italic*/_italic_, `code`. Ordered so ** wins over *.
_INLINE = re.compile(r"(\*\*.+?\*\*|__.+?__|\*.+?\*|_.+?_|`.+?`)", re.DOTALL)


@lru_cache(maxsize=32)
def _font(bold: bool, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_BOLD if bold else FONT_REGULAR), size)


@lru_cache(maxsize=32)
def _metrics(size: int) -> tuple[int, int, int]:
    """Return ``(pitch, ink_top, ink_bottom)`` for a font size.

    Pillow anchors ``draw.text`` at the ascender, not at the ink, so laying
    lines out on ``size`` alone leaves boxed runs overlapping the row above.
    The "Ag" band gives the real cap-to-descender extent; deriving both the
    pitch and the code box from it keeps every size visually consistent and
    avoids paying for the font's generous built-in line gap on a small panel.
    """
    font = _font(False, size)
    _, top, _, bottom = font.getbbox("Ag")
    return (bottom - top) + max(3, round(size * LINE_SPACING)), top, bottom


def _split_inline(text: str) -> list[tuple[str, bool, bool]]:
    """Split a line into ``(text, bold, code)`` runs, markers stripped.

    Italic is folded into bold: there is no italic cut of the bundled face,
    and a synthetic skew smears into mush once thresholded. Emphasis of
    either kind therefore renders as SemiBold.
    """
    runs: list[tuple[str, bool, bool]] = []
    for part in _INLINE.split(text):
        if not part:
            continue
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            runs.append((part[1:-1], False, True))
        elif part[:2] in ("**", "__") and len(part) > 4:
            runs.append((part[2:-2], True, False))
        elif part[0] in "*_" and len(part) > 2:
            runs.append((part[1:-1], True, False))
        else:
            runs.append((part, False, False))
    return runs


def _wrap(runs, size, max_width):
    """Greedily wrap ``runs`` to ``max_width``, returning a list of run lists.

    Wrapping happens across runs, not within them, so a bold word mid-sentence
    does not force a break. Words wider than the whole line are hard-split so
    a pasted URL cannot run off the panel.
    """
    lines: list[list[tuple[str, bool, bool]]] = [[]]
    width = 0.0
    for text, bold, code in runs:
        font = _font(bold, size)
        # Keep the spaces as their own tokens so run boundaries survive.
        for token in re.split(r"(\s+)", text):
            if not token:
                continue
            token_width = font.getlength(token)
            if token.isspace():
                if lines[-1]:  # never start a line with a space
                    lines[-1].append((" ", bold, code))
                    width += font.getlength(" ")
                continue
            if width and width + token_width > max_width:
                lines.append([])
                width = 0.0
            while token_width > max_width:
                # Too wide even on an empty line (a pasted URL, say): bite off
                # what fits rather than letting it run off the panel. `cut` is
                # always at least 1, so this terminates.
                cut = len(token)
                while cut > 1 and font.getlength(token[:cut]) > max_width:
                    cut -= 1
                lines[-1].append((token[:cut], bold, code))
                lines.append([])
                token = token[cut:]
                token_width = font.getlength(token)
            if token:
                lines[-1].append((token, bold, code))
                width += token_width
    return [line for line in lines if line]


def _draw_line(draw, x, y, line, size) -> int:
    """Draw one wrapped line at ``(x, y)``; return its height."""
    height = 0
    for text, bold, code in line:
        font = _font(bold, size)
        length = font.getlength(text)
        if code:
            # Inverted box: the only differentiator that survives 1bpp.
            _, top, bottom = _metrics(size)
            draw.rectangle((x - 1, y + top - 1, x + length + 1, y + bottom), fill=0)
            draw.text((x, y), text, font=font, fill=255)
        else:
            draw.text((x, y), text, font=font, fill=0)
        x += length
        height = max(height, size)
    return height


def render_image(text: str, size: tuple[int, int] = (400, 300)) -> Image.Image:
    """Render Markdown to a mode-"1" image of exactly ``size``."""
    width, height = size
    canvas = Image.new("L", size, 255)
    draw = ImageDraw.Draw(canvas)
    y = MARGIN
    bottom = height - MARGIN
    clipped = False

    for raw in text.splitlines():
        if y >= bottom:
            clipped = bool(raw.strip())
            break

        line = raw.rstrip()
        indent = 0
        prefix = ""
        font_size = BODY_SIZE
        bold_all = False

        if not line.strip():
            y += PARAGRAPH_GAP
            continue

        if _RULE.match(line):
            rule_y = y + PARAGRAPH_GAP
            draw.line((MARGIN, rule_y, width - MARGIN, rule_y), fill=0)
            y = rule_y + PARAGRAPH_GAP + 1
            continue

        if match := _HEADING.match(line):
            level = min(len(match.group(1)), 3)
            font_size = HEADING_SIZES[level]
            bold_all = True
            line = match.group(2)
        elif match := _QUOTE.match(line):
            indent = INDENT
            line = match.group(1)
            draw.line(
                (MARGIN + 3, y + _metrics(BODY_SIZE)[1], MARGIN + 3, y + _metrics(BODY_SIZE)[2]),
                fill=0,
                width=2,
            )
        elif match := _BULLET.match(line):
            prefix = "• "
            indent = INDENT
            line = match.group(1)
        elif match := _ORDERED.match(line):
            prefix = f"{match.group(1)}. "
            indent = INDENT
            line = match.group(2)

        runs = _split_inline(line)
        if bold_all:
            runs = [(t, True, c) for t, _, c in runs]

        x0 = MARGIN + indent
        if prefix:
            # The marker sits in the indent, so wrapped lines hang under the
            # text rather than under the bullet.
            draw.text(
                (MARGIN, y), prefix, font=_font(bold_all, font_size), fill=0
            )

        pitch, _, ink_bottom = _metrics(font_size)
        for wrapped in _wrap(runs, font_size, width - MARGIN - x0):
            if y + ink_bottom > bottom:
                clipped = True
                break
            _draw_line(draw, x0, y, wrapped, font_size)
            y += pitch
        if clipped:
            break

    if clipped:
        # Tell the reader something is missing rather than ending mid-sentence.
        font = _font(True, BODY_SIZE)
        draw.text(
            (width - MARGIN - font.getlength("…"), bottom - BODY_SIZE),
            "…",
            font=font,
            fill=0,
        )

    return canvas.point(lambda v: 255 if v >= INK_CUTOFF else 0, mode="1")


def pack_1bpp(image: Image.Image) -> bytes:
    """Pack a mode-"1" image MSB-first, one set bit per black pixel.

    Pillow packs mode "1" with a set bit meaning *white*, the opposite of the
    firmware's wire format, hence the complement. Ported from
    ``tools/epaper_push.py``.
    """
    return bytes(b ^ 0xFF for b in image.tobytes())


def unpack_1bpp(packed: bytes, size: tuple[int, int]) -> Image.Image:
    """Inverse of :func:`pack_1bpp`, for verifying a round trip."""
    return Image.frombytes("1", size, bytes(b ^ 0xFF for b in packed))


def render_markdown(text: str, size: tuple[int, int] = (400, 300)) -> bytes:
    """Render Markdown straight to the payload the firmware expects."""
    return pack_1bpp(render_image(text, size))


if __name__ == "__main__":
    import sys

    source = Path(sys.argv[1]).read_text() if len(sys.argv) > 1 else "# Hello\n\nWorld"
    out = sys.argv[2] if len(sys.argv) > 2 else "preview.png"
    render_image(source).save(out)
    print(f"wrote {out}")
