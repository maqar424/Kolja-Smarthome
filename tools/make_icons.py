#!/usr/bin/env python3
"""Draw the app icon (same design as static/icon.svg) as PNG, no image library needed.

    python tools/make_icons.py   # writes static/icon-192.png and static/icon-512.png
"""

import struct
import zlib
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "static"
BG = (16, 19, 23)
SUN = (240, 160, 75)
GLOW = (240, 160, 75, 0.18)
BAR1 = (201, 206, 214)
BAR2 = (152, 161, 173)
SAMPLES = 4  # supersampling per axis for smooth edges


def in_round_rect(x, y, rx, ry, w, h, r):
    if not (rx <= x <= rx + w and ry <= y <= ry + h):
        return False
    cx = min(max(x, rx + r), rx + w - r)
    cy = min(max(y, ry + r), ry + h - r)
    return (x - cx) ** 2 + (y - cy) ** 2 <= r * r


def colour_at(x, y):
    """Colour of the 512x512 design at (x, y), or None outside the rounded square."""
    if not in_round_rect(x, y, 0, 0, 512, 512, 112):
        return None
    colour = BG
    d2 = (x - 256) ** 2 + (y - 232) ** 2
    if d2 <= 150 ** 2:
        a = GLOW[3]
        colour = tuple(round(c * (1 - a) + g * a) for c, g in zip(colour, GLOW[:3]))
    if d2 <= 96 ** 2:
        colour = SUN
    if in_round_rect(x, y, 216, 340, 80, 22, 11):
        colour = BAR1
    if in_round_rect(x, y, 226, 374, 60, 22, 11):
        colour = BAR2
    return colour


def render(size):
    scale = 512 / size
    rows = []
    for py in range(size):
        row = bytearray([0])  # filter type 0
        for px in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(SAMPLES):
                for sx in range(SAMPLES):
                    x = (px + (sx + 0.5) / SAMPLES) * scale
                    y = (py + (sy + 0.5) / SAMPLES) * scale
                    c = colour_at(x, y)
                    if c is not None:
                        acc[0] += c[0]; acc[1] += c[1]; acc[2] += c[2]; acc[3] += 1
            n = SAMPLES * SAMPLES
            if acc[3]:
                row += bytes([acc[0] // acc[3], acc[1] // acc[3], acc[2] // acc[3], round(255 * acc[3] / n)])
            else:
                row += bytes(4)
        rows.append(bytes(row))
    return png(size, size, b"".join(rows))


def png(width, height, raw):
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)  # 8-bit RGBA
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


if __name__ == "__main__":
    for size in (192, 512):
        path = STATIC / f"icon-{size}.png"
        path.write_bytes(render(size))
        print(f"wrote {path.relative_to(STATIC.parent)}")
