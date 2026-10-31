# SPDX-License-Identifier: MIT
"""Minimal PNG writer for RGB555 screenshots (standard library only)."""
import struct
import zlib


def rgb555_to_png(pixels, width, height):
    rows = bytearray()
    for y in range(height):
        rows.append(0)  # filter: none
        base = y * width * 2
        for x in range(width):
            v = pixels[base + x * 2] | pixels[base + x * 2 + 1] << 8
            r, g, b = v & 31, (v >> 5) & 31, (v >> 10) & 31
            rows += bytes(((r << 3) | (r >> 2), (g << 3) | (g >> 2), (b << 3) | (b >> 2)))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(bytes(rows), 6))
            + chunk(b"IEND", b""))
