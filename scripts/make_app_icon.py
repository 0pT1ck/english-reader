"""Draw the app icon, without an image library.

图标 app icon / 资源目录 asset catalog

**Why this exists as a script rather than a committed PNG.** The icon is
generated from the few numbers below, so changing it is editing a colour rather
than opening an image editor nobody on this machine has. The PNG *is* committed
— Xcode needs a file, not a recipe — and this script is how it was made.

**Why no Pillow.** It is not installed and adding a dependency to draw eleven
rectangles is not worth it. PNG is a simple enough container to write by hand:
a fixed header, one IDAT chunk of zlib-compressed scanlines, and CRC32s.

**What it draws, and why that.** Lines of text with one word marked — which is
the whole product in one picture. No glyphs: a Chinese character would need a
font file and a font raster, and the metaphor does not need one.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

SIZE = 1024

#: Ink on paper, with the mark in the one colour the app uses for "you marked
#: this". Dark ground so the icon reads on both light and dark home screens.
GROUND = (0x14, 0x16, 0x1F)
LINE = (0xE8, 0xE9, 0xF0)
FADED = (0x50, 0x55, 0x66)
MARK = (0x4A, 0x8C, 0xFF)

#: (y, x, width, colour) for each bar, as fractions of the canvas.
#: Four lines of text; on the third, one short bar is the marked word.
BARS = [
    (0.255, 0.175, 0.650, LINE),
    (0.355, 0.175, 0.560, LINE),
    (0.455, 0.175, 0.170, LINE),
    (0.455, 0.375, 0.215, MARK),      # the marked word
    (0.455, 0.620, 0.205, LINE),
    (0.555, 0.175, 0.480, FADED),
    (0.655, 0.175, 0.330, FADED),
]
BAR_HEIGHT = 0.055
RADIUS = 0.0275                        # half the bar height — fully rounded ends


def rounded_bar(px: list[list[tuple[int, int, int]]], top: int, left: int,
                width: int, height: int, colour: tuple[int, int, int]) -> None:
    """One bar with round caps. Drawn per-pixel; at 1024² that is fast enough."""
    radius = height / 2
    for y in range(top, min(top + height, SIZE)):
        dy = abs((y - top) - radius + 0.5)
        # Horizontal inset at this row so the ends are round rather than square.
        inset = radius - (radius ** 2 - dy ** 2) ** 0.5 if dy < radius else radius
        start = max(0, int(left + inset))
        end = min(SIZE, int(left + width - inset))
        row = px[y]
        for x in range(start, end):
            row[x] = colour


def main() -> int:
    px = [[GROUND] * SIZE for _ in range(SIZE)]
    height = int(BAR_HEIGHT * SIZE)
    for top, left, width, colour in BARS:
        rounded_bar(px, int(top * SIZE), int(left * SIZE), int(width * SIZE),
                    height, colour)

    # Scanlines, each prefixed with filter byte 0 (None). No alpha: **an iOS app
    # icon must be fully opaque**, and a transparent one is rejected at upload
    # with a message that does not say "transparent".
    raw = bytearray()
    for row in px:
        raw.append(0)
        for r, g, b in row:
            raw += bytes((r, g, b))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", SIZE, SIZE, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")

    out = Path("client/App/Assets.xcassets/AppIcon.appiconset/icon-1024.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(png)
    print(f"{out} — {len(png) / 1024:.0f} KB, {SIZE}x{SIZE}, 不透明")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
