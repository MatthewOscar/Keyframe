# Keyframe visual system

Keyframe's mark combines three ideas: a film frame for the source, code brackets
for developer context, and an orange cursor for the moment selected from time.
The identity is intentionally compact enough for an MCP tool/plugin card.

## Assets

| Asset | Use |
| --- | --- |
| `plugins/keyframe/assets/icon.svg` | Square composer icon on either theme. |
| `plugins/keyframe/assets/logo-light.svg` | Wordmark on white or light surfaces. |
| `plugins/keyframe/assets/logo-dark.svg` | Wordmark on charcoal or dark surfaces. |
| `docs/design/keyframe-workflow.png` | 16:9 product-workflow concept. |

All three SVGs contain accessible `<title>` and `<desc>` elements, use vector
geometry, and require no external raster or font asset. The wordmarks request a
system sans-serif stack; the symbol itself is font-independent.

## Palette

- Ink: `#0B1220`
- Slate text: `#0F172A`
- Keyframe teal: `#2DD4BF`
- Light teal: `#99F6E4`
- Cursor amber: `#F59E0B`
- Light foreground: `#F8FAFC`

Do not place the light-theme wordmark on dark media or the dark-theme wordmark
on light media. Preserve at least one-quarter of the icon width as clear space.
Do not recolor the cursor separately from the amber accent.

## Product-workflow concept

![Keyframe product-story concept](keyframe-workflow.png)

This generated raster is a launch-story illustration, not a screenshot of a
hosted Keyframe UI. It visualizes the real workflow—filmstrip moments, separate
said/shown evidence, a verified source frame, a code edit, and passing tests—
without implying that v0.1 ships a gallery application. It was generated with
the built-in image tool after the MCP acceptance suite passed and then copied
into the repository as a visual explanation of the product workflow.

Prompt direction: a premium dark developer-tool launch graphic with a
left-to-right filmstrip → evidence → verified code flow, near-black/graphite
surfaces, restrained teal/cyan/amber accents, the title “KEYFRAME,” small
“SAID,” “SHOWN,” and “VERIFIED” labels, and no people or third-party logos.

## Deferred interactive gallery direction

The post-v0.1 gallery should look like an evidence browser, not a video editor:
a timeline of retained moments on the left, a large source frame in the center,
and timestamped transcript/OCR evidence on the right. Confidence and parse
status remain visible next to every reconstruction. Do not build this surface
unless it remains clearly behind the six local tools; they are the product's
primary interaction surface.
