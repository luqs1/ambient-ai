"""Draw Ambient's icons from the faceted-mountain mark (same polygons as the site logo).

    uv run --with pillow python make_icons.py app-icon.png src-tauri/icons/tray.png
    bunx tauri icon app-icon.png        # then regenerate every app icon size from it

app-icon.png  1024px full-colour mark on a macOS-sized canvas (824px artwork, 100px margin),
              with a misregistered-print shadow like the site's cards.
tray.png      44px (22pt @2x) monochrome template for the menu bar: macOS recolours it.
"""
import sys

from PIL import Image, ImageDraw

# The mark lives on a 64-unit grid (see site/public/index.html, .brand svg).
TILE = [(10, 0), (54, 0), (64, 10), (64, 54), (54, 64), (10, 64), (0, 54), (0, 10)]
COLOURED = [
    ("#2b27c9", TILE),                                               # ultramarine sky
    ("#1e86b0", [(0, 34), (64, 30), (64, 54), (54, 64), (10, 64), (0, 54)]),
    ("#ff8a6a", [(46, 9), (52, 15), (46, 21), (40, 15)]),            # diamond sun
    ("#b6a7f0", [(4, 52), (26, 16), (28, 52)]),                      # far peak, lit face
    ("#6a4fd0", [(26, 16), (48, 52), (28, 52)]),                     # far peak, shade
    ("#c8e64a", [(28, 58), (44, 30), (45, 58)]),                     # near peak, lit
    ("#8dd05a", [(44, 30), (62, 58), (45, 58)]),                     # near peak, shade
    ("#c8e64a", [(0, 54), (64, 50), (64, 54), (54, 64), (10, 64), (0, 54)]),  # meadow
    ("#62e6d8", [(22, 64), (31, 52), (35, 52), (29, 64)]),           # river
]


def scaled(poly, k, ox=0, oy=0):
    return [(ox + x * k, oy + y * k) for x, y in poly]


def app_icon(path: str):
    S, art, margin = 1024, 824, 100
    k = art / 64
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    shadow = 22
    d.polygon(scaled(TILE, k, margin + shadow, margin + shadow), fill="#1b1747")
    for colour, poly in COLOURED:
        d.polygon(scaled(poly, k, margin, margin), fill=colour)
    img.save(path)


def tray_icon(path: str):
    # Draw big (4x) on the 64 grid, then downsample: crisp edges at 44px.
    T, up = 44, 8
    big = T * up
    pad = 2 * up                        # breathing room inside the 22pt slot
    k = (big - 2 * pad) / 64
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    solid, shade, clear = (0, 0, 0, 255), (0, 0, 0, 120), (0, 0, 0, 0)
    P = lambda poly: scaled(poly, k, pad, pad)  # noqa: E731
    # far peak: lit face solid, shaded face half-tone (template icons keep alpha)
    d.polygon(P([(2, 58), (24, 14), (27, 58)]), fill=solid)
    d.polygon(P([(24, 14), (46, 58), (27, 58)]), fill=shade)
    # knock a gap around the near peak so the two read as separate facets
    d.polygon(P([(24, 62), (43, 26), (66, 62)]), fill=clear)
    d.polygon(P([(30, 58), (43, 32), (44, 58)]), fill=solid)
    d.polygon(P([(43, 32), (62, 58), (44, 58)]), fill=shade)
    # ground
    d.polygon(P([(0, 59), (64, 59), (64, 63), (0, 63)]), fill=solid)
    # diamond sun
    d.polygon(P([(50, 2), (57, 9), (50, 16), (43, 9)]), fill=solid)
    img.resize((T, T), Image.LANCZOS).save(path)


if __name__ == "__main__":
    app_icon(sys.argv[1])
    tray_icon(sys.argv[2])
