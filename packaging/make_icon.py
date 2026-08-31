"""Generate packaging/magoo.ico from the wordmark used in the web UI.

Draws the same teal rounded outline with a filled centre square that
base.html renders as an inline SVG favicon, so the taskbar icon and the
browser tab match.

Pure standard library on purpose: an icon is not worth a Pillow dependency
in the build environment, and a script with no dependencies still runs in
five years. Run it only when the mark changes; the .ico is committed.

    .venv/Scripts/python.exe packaging/make_icon.py
"""

import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent / "magoo.ico"

# Direction B "console" palette, from DESIGN.md.
ACCENT = (63, 193, 201, 255)   # #3fc1c9
BACKDROP = (10, 16, 20, 255)   # #0a1014
CLEAR = (0, 0, 0, 0)

SIZES = (16, 24, 32, 48, 64, 128, 256)


def draw(size: int) -> bytes:
    """RGBA pixel rows for one square icon."""
    # Proportions scaled from the 16px SVG: a 13/16 rounded frame two units
    # thick, with a 6/16 solid square centred inside it.
    unit = size / 16.0
    inset = 1.5 * unit
    thickness = max(1.0, 2.0 * unit)
    radius = 2.0 * unit
    outer_lo, outer_hi = inset, size - inset
    inner_lo, inner_hi = inset + thickness, size - inset - thickness
    square_lo, square_hi = 5.0 * unit, 11.0 * unit

    def in_rounded(x, y, lo, hi, r):
        if not (lo <= x <= hi and lo <= y <= hi):
            return False
        # Only the corners are curved; everything else is inside the box.
        cx = lo + r if x < lo + r else (hi - r if x > hi - r else x)
        cy = lo + r if y < lo + r else (hi - r if y > hi - r else y)
        if cx == x and cy == y:
            return True
        return (x - cx) ** 2 + (y - cy) ** 2 <= r * r

    rows = []
    for py in range(size):
        row = bytearray()
        for px in range(size):
            x, y = px + 0.5, py + 0.5
            if square_lo <= x <= square_hi and square_lo <= y <= square_hi:
                pixel = ACCENT
            elif in_rounded(x, y, outer_lo, outer_hi, radius) and not in_rounded(
                x, y, inner_lo, inner_hi, max(0.0, radius - thickness)
            ):
                pixel = ACCENT
            elif in_rounded(x, y, outer_lo, outer_hi, radius):
                pixel = BACKDROP
            else:
                pixel = CLEAR
            row += bytes(pixel)
        rows.append(bytes(row))
    return b"".join(rows)


def to_png(size: int, pixels: bytes) -> bytes:
    """Minimal RGBA PNG. Windows has accepted PNG-compressed icon entries
    since Vista, which avoids hand-rolling the BMP/AND-mask format."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        body = tag + payload
        return (
            struct.pack(">I", len(payload))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    stride = size * 4
    raw = b"".join(
        b"\x00" + pixels[i : i + stride] for i in range(0, len(pixels), stride)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def main() -> None:
    images = [(size, to_png(size, draw(size))) for size in SIZES]
    offset = 6 + 16 * len(images)
    header = struct.pack("<HHH", 0, 1, len(images))
    entries, blobs = b"", b""
    for size, data in images:
        entries += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,  # 0 means 256 in the ICO format
            0 if size >= 256 else size,
            0,
            0,
            1,
            32,
            len(data),
            offset,
        )
        blobs += data
        offset += len(data)
    OUT.write_bytes(header + entries + blobs)
    print(f"wrote {OUT} ({OUT.stat().st_size:,} bytes, {len(images)} sizes)")


if __name__ == "__main__":
    main()
