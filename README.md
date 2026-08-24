# ESPaper Uploader

A Home Assistant integration for the [BLE e-paper display](../README.md): an
ESP32-H2 driving a 400×300 monochrome panel.

You get a **text entity**. Put Markdown in it, and the integration renders it to
a 1bpp frame and pushes it to the panel the next time the panel wakes up. Once
the panel is showing the current text, nothing further happens — no polling, no
reconnecting — until you change the text again.

<p align="center">
  <img src="docs/preview.png" alt="The panel showing a rendered shopping list" width="436">
</p>

That panel is [this Markdown](docs/sample.md):

```markdown
# Groceries

Pick up on the way home from **Netto**, before 8pm.

- Milk, 2 L
- 500 g of `flour`
- A really long item name that has to wrap onto the next line neatly
1. Bread
2. Eggs

## Notes
> The panel refreshes about once a minute, and holds the image with no power.

Set from an automation with `espaper.set_markdown`.
```

## Why it works this way

The board advertises for about two seconds and then deep-sleeps for a minute, so
an upload cannot be scheduled — it has to be caught. Every advertisement Home
Assistant's Bluetooth stack sees is an opportunity, and the integration takes it
only while it actually owes the panel a frame. An update therefore lands within
about one sleep cycle (~60 s by default), and a failed transfer simply retries on
the next wake.

E-paper holds its image without power, so after a Home Assistant restart the
integration compares the restored text with the text it last confirmed on the
panel and stays quiet if they match.

## Install

**HACS** → three-dot menu → *Custom repositories* → add
`https://github.com/n0n3m4/espaper-uploader` as an *Integration*, then install
and restart Home Assistant.

Or copy `custom_components/espaper` into your `config/custom_components/`.

The panel is then auto-discovered (it advertises its vendor service UUID) and
shows up as a Bluetooth discovery in **Settings → Devices & Services** within a
wake cycle. There is no pairing and no token.

## Use

The device gets one entity, `text.espaper_markdown`:

```yaml
automation:
  - trigger:
      - platform: state
        entity_id: sensor.next_bin_collection
    action:
      - action: text.set_value
        target:
          entity_id: text.espaper_markdown
        data:
          value: "# Bins\n\nNext: {{ states('sensor.next_bin_collection') }}"
```

Home Assistant truncates any entity state at 255 characters. For longer
documents use the service, which has no such limit:

```yaml
      - action: espaper.set_markdown
        target:
          entity_id: text.espaper_markdown
        data:
          markdown: >
            # Today

            {% for e in state_attr('calendar.family', 'events') %}
            - {{ e.summary }}
            {% endfor %}
```

The entity's attributes show `upload_status` (`idle`, `pending`, `uploading`),
`last_upload`, `last_error`, and `uploaded_markdown` — what the panel is actually
displaying right now.

## Supported Markdown

A deliberate subset, chosen for what stays legible on a 400×300 1bpp panel:

| Syntax | Rendered as |
|---|---|
| `# ## ###` | Headings, 26/21/17 px semibold; `#` and `##` get a rule under them |
| `- item` / `* item` | Bullet, hanging indent |
| `1. item` | Numbered, hanging indent |
| `**bold**`, `*italic*` | Semibold (see below) |
| `` `code` `` | White-on-black box |
| `> quote` | Indented, with a bar down the whole quote |
| `---` | Horizontal rule |
| blank line | Paragraph gap |

Text that runs past the bottom of the panel is clipped, and a `…` is drawn in the
corner so it is obvious something was cut.

There is no italic cut of the bundled font, and a synthetic skew turns to mush
once thresholded to one bit, so `*italic*` renders as semibold — emphasis is
shown, just not distinguished from `**bold**`.

## Rendering notes

The font is **Noto Sans SemiCondensed Medium**, bundled from
[Inkycal](https://github.com/aceinnolab/Inkycal) (SIL Open Font License, see
`custom_components/espaper/fonts/`). Semicondensed fits noticeably more
characters per line; *Medium* rather than Regular because Regular's thin stems
break up when antialiased greys are thresholded to black and white.

Two other things matter more than they look like they should:

- Glyphs are drawn antialiased on a greyscale canvas and then thresholded at
  160, not 128. Biasing above mid-grey turns marginal edge pixels into ink,
  which thickens stems slightly instead of eroding them.
- Line pitch comes from the *ink* band (`getbbox("Ag")`), not from the font's
  ascent + descent. Noto's built-in line gap is generous, and on a 300 px panel
  that generosity costs about three lines of text.

Structure is carried by rules rather than whitespace — a 300 px panel cannot
spare the blank pixels it would take to separate sections by margin alone, so
`#` and `##` get a hairline under them and block quotes get a bar down the side.

Regenerate the image above with `python docs/make_preview.py` after changing
anything in `render.py`.

## Development

```sh
python test_render.py       # layout and packing; needs only Pillow
python test_coordinator.py  # the upload-once latch; needs homeassistant importable
```

Preview a layout without any hardware:

```sh
python custom_components/espaper/render.py notes.md preview.png
```

Push to a real panel without Home Assistant, to check the protocol port against
the firmware:

```sh
python custom_components/espaper/epaper.py notes.md
```

## Licence

MIT, except the bundled Noto fonts, which are under the SIL Open Font License
1.1 (`custom_components/espaper/fonts/LICENSE_OFL.txt`).
