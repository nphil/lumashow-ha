"""Deterministic Pillow renderer for the LumaShow integration's Home Assistant brand images.

Writes the eight images Home Assistant serves straight from ``custom_components/lumashow/brand/`` (no brands-repository submission needed):

    icon.png        256x256      dark_icon.png        256x256
    icon@2x.png     512x512      dark_icon@2x.png     512x512
    logo.png        864x256      dark_logo.png        864x256
    logo@2x.png     1728x512     dark_logo@2x.png     1728x512

The mark is the LumaShow app icon (``../icon.svg``, hand-authored vector art; this script does not read it): a dark rounded tile with the
glass "light path" L and its warm core. The light variants are that tile. The ``dark_*`` variants (for Home Assistant's dark header) use a
pale plate with a deeper violet mark so they keep their contrast. The logos put the mark beside the LumaShow wordmark, drawn from the same
stroke paths as the Show's own lockup, on a transparent background.

Pillow only, 4x supersampling, no randomness: running it again gives byte-identical PNGs.

Usage: python3 tools/render_brand.py
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from itertools import pairwise
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

BRAND_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "lumashow" / "brand"

SS = 4  # supersampling
REF = 512.0  # the reference space the icon is drawn in (the same numbers as icon.svg)

RGBA = tuple[int, int, int, int]
Stop = tuple[float, RGBA]


def rgba(hex_color: str, alpha: float = 1.0) -> RGBA:
    hex_color = hex_color.lstrip("#")
    return (int(hex_color[0:2], 16), int(hex_color[2:4], 16), int(hex_color[4:6], 16), round(alpha * 255))


# -- palettes ---------------------------------------------------------------------------------------------------------------------------

LIGHT = {  # a dark tile with the glowing glass mark ("icon", "logo")
    "tile": [(0.0, rgba("#26233A")), (0.52, rgba("#1F1D2E")), (1.0, rgba("#393552"))],
    "tile_glow": rgba("#A79BF5", 0.24),
    "bloom": rgba("#A79BF5", 0.48),
    "face": [(0.0, rgba("#8E7FDB", 0.83)), (0.35, rgba("#A79BF5", 0.92)), (0.68, rgba("#F2EFF6", 0.97)), (1.0, rgba("#9CCFD8", 0.82))],
    "line_a": rgba("#FFFAF3", 0.62),
    "line_b": rgba("#FFFFFF", 0.44),
    "line_c": rgba("#FFFFFF", 0.31),
    "core": [(0.0, rgba("#F6C177", 0.82)), (0.38, rgba("#EBBCBA", 0.4)), (1.0, rgba("#A79BF5", 0.0))],
    "dot": rgba("#FFFAF3", 0.96),
    "wordmark": rgba("#26233A"),
}
DARK = {  # a pale tile with a deeper mark ("dark_icon", "dark_logo")
    "tile": [(0.0, rgba("#DDD7FF")), (0.52, rgba("#F2EFF6")), (1.0, rgba("#FFFFFF"))],
    "tile_glow": rgba("#8E7FDB", 0.14),
    "bloom": rgba("#5A4FB5", 0.34),
    "face": [(0.0, rgba("#3E3590", 0.95)), (0.35, rgba("#5A4FB5", 0.97)), (0.68, rgba("#8E7FDB", 0.97)), (1.0, rgba("#4C93A3", 0.92))],
    "line_a": rgba("#F2EFF6", 0.7),
    "line_b": rgba("#F2EFF6", 0.5),
    "line_c": rgba("#F2EFF6", 0.36),
    "core": [(0.0, rgba("#F6C177", 0.9)), (0.38, rgba("#EBBCBA", 0.45)), (1.0, rgba("#A79BF5", 0.0))],
    "dot": rgba("#FFFAF3", 0.98),
    "wordmark": rgba("#F2EFF6"),
}

# -- geometry (icon.svg's numbers) --------------------------------------------------------------------------------------------------------

Point = tuple[float, float]
# The mark, in its own 240 space: down, round the corner, along.
MARK_PATH = "M76 47V121C76 154 102 180 135 180H196"
MARK_LINE_B = "M66 48V120C66 149 89 171 117 171H181"
MARK_LINE_C = "M84 55V118C84 146 105 164 133 164H175"
MARK_BBOX = (76.0, 47.0, 196.0, 180.0)  # what the face's gradient is measured against (the path's own box)
MARK_CORE = (110.0, 160.0, 28.0)
MARK_DOT = (107.0, 157.0, 2.4)
TILE_RADIUS = 112.0
MARK_SCALE, MARK_DY = 1.7, 51.0  # translate(0 51) scale(1.7)

WORDMARK_PATHS = (
    "M10 8V53Q10 59 16 59H37",
    "M50 25V42Q50 59 65 59Q80 59 80 42V25",
    "M93 59V25H105Q121 25 121 41V59M121 41Q121 25 137 25Q153 25 153 42V59",
    "M194 35Q189 25 178 25Q162 25 162 42Q162 59 178 59Q193 59 194 44V25M194 42V59",
    "M247 14C241 10 235 8 228 8C217 8 210 14 210 23C210 32 218 36 230 40C242 44 249 48 249 53C249 57 242 59 232 59C224 59 217 56 212 52",
    "M263 8V59M263 41Q262 25 277 25Q292 25 292 41V59",
    "M306 42Q306 25 322 25Q338 25 338 42Q338 59 322 59Q306 59 306 42Z",
    "M351 25L362 59L373 34L384 59L395 25",
)
WORDMARK_STROKE = 7.2
WORDMARK_BOX = (10.0, 8.0, 395.0, 59.0)

TOKEN = re.compile(r"[MVHLQCZ]|-?\d*\.?\d+")


def flatten(path: str) -> list[list[Point]]:
    """The sub-paths of an absolute SVG path (M, L, H, V, Q, C, Z) as polylines."""
    tokens = TOKEN.findall(path)
    out: list[list[Point]] = []
    current: list[Point] = []
    x = y = 0.0
    start = (0.0, 0.0)
    i = 0
    command = ""

    def number() -> float:
        nonlocal i
        value = float(tokens[i])
        i += 1
        return value

    while i < len(tokens):
        if tokens[i] in "MVHLQCZ":
            command = tokens[i]
            i += 1
            if command == "Z":
                current.append(start)
                out.append(current)
                current = []
                x, y = start
                continue
        if command == "M":
            if current:
                out.append(current)
            x, y = number(), number()
            start = (x, y)
            current = [(x, y)]
            command = "L"  # further pairs after an M are lines
        elif command == "L":
            x, y = number(), number()
            current.append((x, y))
        elif command == "H":
            x = number()
            current.append((x, y))
        elif command == "V":
            y = number()
            current.append((x, y))
        elif command == "Q":
            cx, cy, nx, ny = number(), number(), number(), number()
            for k in range(1, 25):
                t = k / 24
                current.append(((1 - t) ** 2 * x + 2 * (1 - t) * t * cx + t * t * nx, (1 - t) ** 2 * y + 2 * (1 - t) * t * cy + t * t * ny))
            x, y = nx, ny
        elif command == "C":
            c1x, c1y, c2x, c2y, nx, ny = (number() for _ in range(6))
            for k in range(1, 41):
                t = k / 40
                a, b, c, d = (1 - t) ** 3, 3 * (1 - t) ** 2 * t, 3 * (1 - t) * t * t, t**3
                current.append((a * x + b * c1x + c * c2x + d * nx, a * y + b * c1y + c * c2y + d * ny))
            x, y = nx, ny
    if current:
        out.append(current)
    return out


# -- drawing ------------------------------------------------------------------------------------------------------------------------------


def lerp_stops(stops: Sequence[Stop], t: float) -> RGBA:
    t = min(1.0, max(0.0, t))
    for (t0, c0), (t1, c1) in pairwise(stops):
        if t <= t1:
            f = 0.0 if t1 == t0 else (t - t0) / (t1 - t0)
            return tuple(round(a + (b - a) * f) for a, b in zip(c0, c1, strict=True))  # type: ignore[return-value]
    return stops[-1][1]


def gradient(size: tuple[int, int], stops: Sequence[Stop], t_of: Callable[[float, float], float]) -> Image.Image:
    """An RGBA image of ``size`` whose colour at (x, y) is the stops at ``t_of(x, y)`` (x, y in pixels): made coarse, then scaled up."""
    w, h = size
    n = 64
    coarse = Image.new("RGBA", (n, n))
    px = coarse.load()
    assert px is not None
    for j in range(n):
        for i in range(n):
            px[i, j] = lerp_stops(stops, t_of((i + 0.5) * w / n, (j + 0.5) * h / n))
    return coarse.resize(size, Image.BICUBIC)


def stroke_mask(size: int | tuple[int, int], polylines: list[list[Point]], width: float) -> Image.Image:
    """An L mask of the polylines drawn with round caps and joins (a disc at every sample: the stroke is their union)."""
    dims = (size, size) if isinstance(size, int) else size
    mask = Image.new("L", dims, 0)
    draw = ImageDraw.Draw(mask)
    r = width / 2
    for line in polylines:
        for (x0, y0), (x1, y1) in pairwise(line):
            steps = max(1, math.ceil(math.hypot(x1 - x0, y1 - y0) / max(r / 12, 0.5)))
            for k in range(steps + 1):
                t = k / steps
                x, y = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
                draw.ellipse((x - r, y - r, x + r, y + r), fill=255)
    return mask


def tinted(mask: Image.Image, colour: RGBA) -> Image.Image:
    layer = Image.new("RGBA", mask.size, (*colour[:3], 0))
    alpha = mask.point(lambda v: v * colour[3] // 255)
    layer.putalpha(alpha)
    return layer


def over(base: Image.Image, layer: Image.Image) -> Image.Image:
    return Image.alpha_composite(base, layer)


def rounded_tile(size: int, palette: dict) -> Image.Image:
    """The tile, SS times the final size: a rounded square with its gradient and the soft glow behind the mark."""
    k = size / REF
    tile = gradient((size, size), palette["tile"], lambda x, y: (x / size + (1 - y / size)) / 2)
    # The glow: a radial fade centred high, as in the app icon.
    glow_colour = palette["tile_glow"]
    glow = Image.new("RGBA", (size, size), (*glow_colour[:3], 0))
    ga = glow.getchannel("A")
    radial = gradient(
        (size, size),
        [(0.0, (255, 255, 255, glow_colour[3])), (1.0, (255, 255, 255, 0))],
        lambda x, y: math.hypot(x - 256 * k, y - 220 * k) / (230 * k),
    ).getchannel("A")
    glow.putalpha(ImageChops.lighter(ga, radial))
    tile = over(tile, glow)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=TILE_RADIUS * k, fill=255)
    tile.putalpha(ImageChops.multiply(tile.getchannel("A"), mask))
    return tile


def draw_mark(canvas: Image.Image, palette: dict, scale: float, dx: float, dy: float) -> Image.Image:
    """The glass L onto ``canvas``: ``scale`` and ``(dx, dy)`` take the mark's 240 space to pixels."""

    def to_px(lines: list[list[Point]]) -> list[list[Point]]:
        return [[(dx + x * scale, dy + y * scale) for x, y in line] for line in lines]

    size = canvas.size
    main = to_px(flatten(MARK_PATH))
    # The bloom round the glass: a wide stroke, blurred.
    bloom = stroke_mask(size, main, 62 * scale).filter(ImageFilter.GaussianBlur(13 * scale))
    canvas = over(canvas, tinted(bloom, palette["bloom"]))
    # The glass: the stroke, filled with a gradient measured against the path's own box.
    x0, y0, x1, y1 = MARK_BBOX
    bx0, by0, bw, bh = dx + x0 * scale, dy + y0 * scale, (x1 - x0) * scale, (y1 - y0) * scale

    def face_t(x: float, y: float) -> float:
        u, v = (x - bx0) / bw, (y - by0) / bh
        return (u * 0.92 + (1 - v)) / (0.92**2 + 1)

    face = gradient(size, palette["face"], face_t)
    face.putalpha(ImageChops.multiply(face.getchannel("A"), stroke_mask(size, main, 43 * scale)))
    canvas = over(canvas, face)
    # The three light lines inside it.
    for path, colour, width in ((MARK_PATH, palette["line_a"], 2.4), (MARK_LINE_B, palette["line_b"], 2.4), (MARK_LINE_C, palette["line_c"], 2.0)):
        canvas = over(canvas, tinted(stroke_mask(size, to_px(flatten(path)), width * scale), colour))
    # The warm core and its dot.
    cx, cy, r = MARK_CORE
    core = Image.new("L", size, 0)
    ImageDraw.Draw(core).ellipse((dx + (cx - r) * scale, dy + (cy - r) * scale, dx + (cx + r) * scale, dy + (cy + r) * scale), fill=255)
    radial = gradient(
        size,
        palette["core"],
        lambda x, y: math.hypot(x - (dx + cx * scale), y - (dy + cy * scale)) / (r * scale),
    )
    radial.putalpha(ImageChops.multiply(radial.getchannel("A"), core.filter(ImageFilter.GaussianBlur(6 * scale)).point(lambda v: v * 9 // 10)))
    canvas = over(canvas, radial)
    px, py, pr = MARK_DOT
    dot = Image.new("L", size, 0)
    ImageDraw.Draw(dot).ellipse((dx + (px - pr) * scale, dy + (py - pr) * scale, dx + (px + pr) * scale, dy + (py + pr) * scale), fill=255)
    return over(canvas, tinted(dot, palette["dot"]))


def icon(size: int, palette: dict) -> Image.Image:
    """The app icon at ``size`` px: the tile, and the mark placed as in icon.svg (scale 1.7 of 512, shifted down by 51)."""
    big = size * SS
    k = big / REF
    canvas = rounded_tile(big, palette)
    canvas = draw_mark(canvas, palette, MARK_SCALE * k, 0.0, MARK_DY * k)
    return canvas.resize((size, size), Image.LANCZOS)


def wordmark(width: int, palette: dict) -> Image.Image:
    """The wordmark on a transparent canvas ``width`` px wide (its height follows), drawn from the Show's own stroke paths."""
    x0, y0, x1, y1 = WORDMARK_BOX
    pad = WORDMARK_STROKE
    scale = width * SS / (x1 - x0 + 2 * pad)
    size = (round((x1 - x0 + 2 * pad) * scale), round((y1 - y0 + 2 * pad) * scale))
    mask = Image.new("L", size, 0)
    for path in WORDMARK_PATHS:
        lines = [[((x - x0 + pad) * scale, (y - y0 + pad) * scale) for x, y in line] for line in flatten(path)]
        mask = ImageChops.lighter(mask, stroke_mask(size, lines, WORDMARK_STROKE * scale))
    layer = tinted(mask, palette["wordmark"])
    return layer.resize((size[0] // SS, size[1] // SS), Image.LANCZOS)


def logo(height: int, palette: dict) -> Image.Image:
    """The mark beside the wordmark on a transparent canvas of 27:8 (864x256 at ``height`` 256)."""
    width = height * 27 // 8
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    mark = icon(height, palette)
    canvas.alpha_composite(mark, (0, 0))
    text = wordmark(round(width - height - height * 0.28 - height * 0.1), palette)
    canvas.alpha_composite(text, (height + round(height * 0.18), (height - text.height) // 2))
    return canvas


def main() -> None:
    BRAND_DIR.mkdir(parents=True, exist_ok=True)
    for prefix, palette in (("", LIGHT), ("dark_", DARK)):
        for suffix, factor in (("", 1), ("@2x", 2)):
            icon(256 * factor, palette).save(BRAND_DIR / f"{prefix}icon{suffix}.png", optimize=True)
            logo(256 * factor, palette).save(BRAND_DIR / f"{prefix}logo{suffix}.png", optimize=True)


if __name__ == "__main__":
    main()
