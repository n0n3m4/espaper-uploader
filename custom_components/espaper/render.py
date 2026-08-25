"""Render a small Markdown subset to an e-paper frame, 1bpp or 4-colour.

No Home Assistant imports: run it directly to preview a layout.

    python render.py notes.md preview.png

Only Pillow is needed. A full Markdown library would hand back an AST and
leave the entire layout pass still to write, so the subset is parsed inline.

Layout happens once, on a greyscale canvas; only the last step differs
between the two depths. The panel has four levels, so the antialiased canvas
can be quantised straight onto them (`render_markdown(..., gray4=True)`, the
integration's default) and glyph edges survive as grey. Squeezing the same
canvas into one bit throws that away, so it compensates.

Everything here is tuned for a small panel rather than a screen:

* the bundled face is Noto Sans SemiCondensed **Medium** -- semicondensed to
  fit more characters per line, Medium because Regular's thin stems drop out
  when antialiased greys get thresholded (Inkycal defaults to it for the same
  reason),
* for 1bpp the canvas is thresholded at `INK_CUTOFF`, which is set above
  mid-grey deliberately: edge pixels that are only slightly grey become ink,
  thickening stems instead of eroding them. 4-colour needs no such trick --
  those same edge pixels simply land on a grey level,
* `code` spans render as white-on-black boxes rather than in a mono face --
  at this size a weight change is invisible but an inverted box is not,
* structure is drawn rather than spaced: headings get a rule under them and
  quotes a bar beside them, because 300 px of height cannot spare the blank
  rows it would take to separate sections by margin alone.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_DIR = Path(__file__).parent / "fonts"
FONT_REGULAR = FONT_DIR / "NotoSans-SemiCondensedMedium.ttf"
FONT_BOLD = FONT_DIR / "NotoSans-SemiCondensedSemiBold.ttf"

# Grey level at or above which a pixel stays white, for 1bpp only. Above 128
# on purpose: it biases antialiased glyph edges towards ink so small text stays
# solid.
INK_CUTOFF = 160

# Luminance of each 2bpp wire level, level 0 first. Measured off the panel: even
# the extremes are paper grey and grey ink, not 255 and 0. The greys are nowhere near
# evenly spaced, which is exactly why quantising has to work against these
# numbers rather than a linear ramp. Film-specific -- retune here if a later
# batch of panels measures differently. Same table as tools/epaper_push.py.
GRAY_LEVELS = (210, 180, 100, 40)

# Fractional on purpose. A stem lands where the outline falls -- the bundled
# face is ttfautohint output, which emits no horizontal instructions -- so the
# size decides whether stems agree with each other, and it is not monotonic:
# 16.375 holds stem-to-stem variation near 5.5% where 16.0 is 8.7% and 17.0 is
# 7.0%, all three at the same 21 px pitch. See CLAUDE.md before retuning.
BODY_SIZE = 16.375
HEADING_SIZES = {1: 29.75, 2: 26, 3: 21}
# A panel this small looks cramped rather than dense with a hairline margin.
MARGIN = 13
# Air above a heading that follows something. Grouping a heading with the text
# under it, rather than floating it midway, is most of what makes the layout
# read as deliberate.
HEADING_LEAD = 7
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
def _font(bold: bool, size: float) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_BOLD if bold else FONT_REGULAR), size)


@lru_cache(maxsize=32)
def _metrics(size: float) -> tuple[int, int, int]:
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


def render_canvas(text: str, size: tuple[int, int] = (400, 300)) -> Image.Image:
    """Render Markdown to an antialiased mode-"L" image of exactly ``size``."""
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
        heading_level = 0
        quoted = False

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
            heading_level = level
            if y > MARGIN:  # not the very first line
                y += HEADING_LEAD
        elif match := _QUOTE.match(line):
            indent = INDENT
            quoted = True
            line = match.group(1)
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

        pitch, ink_top, ink_bottom = _metrics(font_size)
        quote_top = y + ink_top
        for wrapped in _wrap(runs, font_size, width - MARGIN - x0):
            if y + ink_bottom > bottom:
                clipped = True
                break
            _draw_line(draw, x0, y, wrapped, font_size)
            y += pitch
        if quoted and y > quote_top:
            # One bar for the whole quote, not a tick per line.
            draw.rectangle(
                (MARGIN, quote_top, MARGIN + 2, y - pitch + ink_bottom), fill=0
            )
        if 1 <= heading_level <= 2 and not clipped:
            # A rule under the top two levels separates sections far more
            # cheaply than whitespace can on a 300 px panel.
            rule_y = y - pitch + ink_bottom + 3
            draw.line((MARGIN, rule_y, width - MARGIN, rule_y), fill=0)
            y += 6
        if clipped:
            break

    if clipped:
        # Tell the reader something is missing rather than ending mid-sentence.
        font = _font(True, BODY_SIZE)
        mark_w = font.getlength("…")
        x0 = width - MARGIN - mark_w
        # Clear a patch first: the clipped line may already occupy this corner.
        # int(): a fractional y would put this one glyph on a subpixel origin.
        draw.rectangle((x0 - 4, bottom - int(BODY_SIZE) - 2, width, height), fill=255)
        draw.text((x0, bottom - int(BODY_SIZE)), "…", font=font, fill=0)

    return canvas


def threshold(canvas: Image.Image) -> Image.Image:
    """Squeeze a mode-"L" canvas to mode "1" at :data:`INK_CUTOFF`."""
    return canvas.point(lambda v: 255 if v >= INK_CUTOFF else 0, mode="1")


def render_image(text: str, size: tuple[int, int] = (400, 300)) -> Image.Image:
    """Render Markdown to a mode-"1" image of exactly ``size``."""
    return threshold(render_canvas(text, size))


def quantize_gray4(canvas: Image.Image, levels=GRAY_LEVELS) -> bytes:
    """Map a mode-"L" canvas to one wire level (0..3) per pixel, row-major.

    Nearest level with no error diffusion: dithering is for photographs, and on
    text it would scatter noise through every glyph edge that antialiasing just
    placed deliberately.

    The canvas value is read as coverage between the panel's real black and
    white, not as an absolute luminance, because that is what an antialiased
    edge pixel means -- v parts paper out of 255, the rest ink. Those ends are
    40 and 210, so without the rescale everything outside that span collapses
    into the two end levels and a 0..255 ramp comes out with its widest bands at
    the extremes. `tools/epaper_push.py` keeps the same mapping behind
    `--normalize`, off by default, because a photograph is not a coverage map.
    """
    black, white = levels[3], levels[0]
    table = [
        min(range(4), key=lambda i: abs(black + v * (white - black) / 255 - levels[i]))
        for v in range(256)
    ]
    return canvas.point(table, mode="L").tobytes()


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


def pack_2bpp(levels: bytes) -> bytes:
    """Pack one wire level per pixel into 4 pixels per byte, MSB first."""
    if len(levels) % 4:
        raise ValueError(f"{len(levels)} pixels is not a whole number of bytes")
    return bytes(
        (levels[i] << 6) | (levels[i + 1] << 4) | (levels[i + 2] << 2) | levels[i + 3]
        for i in range(0, len(levels), 4)
    )


def unpack_2bpp(
    packed: bytes, size: tuple[int, int], levels=GRAY_LEVELS
) -> Image.Image:
    """Inverse of :func:`pack_2bpp`, painted with the measured luminances."""
    return Image.frombytes(
        "L",
        size,
        bytes(levels[(b >> shift) & 3] for b in packed for shift in (6, 4, 2, 0)),
    )


def render_markdown(
    text: str,
    size: tuple[int, int] = (400, 300),
    gray4: bool = False,
    rotation: int = 0,
) -> bytes:
    """Render Markdown straight to the payload the firmware expects.

    ``rotation`` is how far clockwise the text sits on the panel, for a display
    hung on its side or upside down. The layout runs at the rotated geometry --
    300x400 for a quarter turn -- and the canvas is turned back to the panel's
    own 400x300 at the end, so wrapping and margins are computed for the shape
    the reader actually sees.
    """
    width, height = size
    canvas = render_canvas(text, (height, width) if rotation % 180 else (width, height))
    if rotation:
        canvas = canvas.rotate(-rotation, expand=True)
    if gray4:
        return pack_2bpp(quantize_gray4(canvas))
    return pack_1bpp(threshold(canvas))


if __name__ == "__main__":
    import sys

    source = Path(sys.argv[1]).read_text() if len(sys.argv) > 1 else "# Hello\n\nWorld"
    out = sys.argv[2] if len(sys.argv) > 2 else "preview.png"
    # What the panel really shows: the 4-colour frame, unpacked at its measured
    # luminances rather than the raw canvas.
    unpack_2bpp(render_markdown(source, gray4=True), (400, 300)).save(out)
    print(f"wrote {out}")
