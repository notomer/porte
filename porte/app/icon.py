"""Icon handling for generated macOS app bundles.

Two sources, in order of preference:

1. The Windows executable's own icon (RT_GROUP_ICON -> RT_ICON), which is what
   the user recognises from Explorer. PNG-compressed icons are used directly;
   uncompressed 32/24-bit DIB icons are decoded here.
2. A generated icon when the PE has none, so the bundle still has a distinct
   identity instead of the generic application icon.

The output is always a .icns produced by `iconutil` from a full iconset.
"""
from __future__ import annotations

import shutil
import struct
import subprocess
import zlib
from pathlib import Path

import pefile

ICONSET_SIZES = [(16, 1), (16, 2), (32, 1), (32, 2), (128, 1), (128, 2),
                 (256, 1), (256, 2), (512, 1), (512, 2)]


class IconError(RuntimeError):
    pass


def _png(width: int, height: int, rgba: bytes) -> bytes:
    """Encode 8-bit RGBA pixels (row-major, no padding) as a PNG."""
    stride = width * 4
    if len(rgba) != stride * height:
        raise ValueError("pixel buffer size does not match dimensions")
    raw = b"".join(b"\x00" + rgba[y * stride:(y + 1) * stride]
                   for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def _decode_dib(data: bytes) -> tuple[int, int, bytes] | None:
    """Decode a Windows icon DIB (BITMAPINFOHEADER + BGRA + AND mask)."""
    if len(data) < 40:
        return None
    header_size, width, height2, planes, bitcount = struct.unpack(
        "<IiiHH", data[:16])
    if header_size < 40 or width <= 0 or height2 <= 0 or planes != 1:
        return None
    height = height2 // 2  # XOR + AND halves
    if width > 1024 or height > 1024:
        return None
    if bitcount not in (24, 32):
        return None
    stride = ((width * bitcount + 31) // 32) * 4
    body = data[header_size:]
    if len(body) < stride * height:
        return None
    and_stride = ((width + 31) // 32) * 4
    and_off = stride * height
    out = bytearray(width * height * 4)
    for y in range(height):
        src_row = body[(height - 1 - y) * stride:(height - y) * stride]
        for x in range(width):
            if bitcount == 32:
                b, g, r, a = src_row[x * 4:x * 4 + 4]
            else:
                b, g, r = src_row[x * 3:x * 3 + 3]
                a = 255
            if bitcount == 32 and a == 0:
                # Icons often store transparency only in the AND mask.
                mask_row = body[and_off + (height - 1 - y) * and_stride:]
                byte = mask_row[x // 8] if x // 8 < len(mask_row) else 0
                a = 0 if (byte >> (7 - (x % 8))) & 1 else 255
            off = (y * width + x) * 4
            out[off:off + 4] = bytes((r, g, b, a))
    if bitcount == 24:
        for i in range(3, len(out), 4):
            out[i] = 255
    return width, height, bytes(out)


def _group_icons(pe: pefile.PE) -> list[dict]:
    """RT_GROUP_ICON entries with the RT_ICON bytes each one references."""
    icons: dict[int, bytes] = {}
    groups: list[dict] = []
    try:
        resources = pe.DIRECTORY_ENTRY_RESOURCE.entries
    except AttributeError:
        return []
    for entry in resources:
        if entry.id != 3 or not hasattr(entry, "directory"):
            continue
        for name_entry in entry.directory.entries:
            for lang in name_entry.directory.entries:
                if not hasattr(lang, "data"):
                    continue
                blob = pe.get_data(lang.data.struct.OffsetToData,
                                   lang.data.struct.Size)
                if name_entry.id is not None:
                    icons[name_entry.id] = blob
    for entry in resources:
        if entry.id != 14 or not hasattr(entry, "directory"):
            continue
        for name_entry in entry.directory.entries:
            for lang in name_entry.directory.entries:
                if not hasattr(lang, "data"):
                    continue
                blob = pe.get_data(lang.data.struct.OffsetToData,
                                   lang.data.struct.Size)
                if len(blob) < 6:
                    continue
                reserved, kind, count = struct.unpack("<HHH", blob[:6])
                entries = []
                for i in range(count):
                    off = 6 + i * 14
                    if off + 14 > len(blob):
                        break
                    w, h, colors, _, planes, bits, size, ident = struct.unpack(
                        "<BBBBHHIH", blob[off:off + 14])
                    if ident not in icons:
                        continue
                    entries.append({"width": w or 256, "height": h or 256,
                                    "bits": bits, "size": size, "id": ident,
                                    "data": icons[ident]})
                if entries:
                    groups.append({"id": name_entry.id, "entries": entries})
    return groups


def extract_pe_icon_png(pe_path: Path) -> tuple[bytes, int] | None:
    """Largest usable icon in the image as PNG bytes, or None."""
    pe = pefile.PE(str(pe_path), fast_load=True)
    try:
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]])
        groups = _group_icons(pe)
    finally:
        pe.close()
    for group in groups:
        for entry in sorted(group["entries"],
                            key=lambda e: (e["width"], e["bits"]), reverse=True):
            data = entry["data"]
            if data.startswith(b"\x89PNG\r\n\x1a\n"):
                return data, entry["width"]
            decoded = _decode_dib(data)
            if decoded is None:
                continue
            width, height, rgba = decoded
            return _png(width, height, rgba), width
    return None


def default_icon_png(size: int = 1024) -> bytes:
    """Generated identity icon: two overlapping windows on a dark field."""
    px = bytearray(size * size * 4)
    back = (46, 52, 64)
    front = (235, 238, 245)
    accent = (74, 144, 226)
    for y in range(size):
        t = y / max(1, size - 1)
        bg = (int(18 + 10 * t), int(20 + 12 * t), int(28 + 14 * t))
        for x in range(size):
            off = (y * size + x) * 4
            px[off:off + 4] = bytes((*bg, 255))

    def rect(x0, y0, x1, y1, color, border=None, width=0):
        for y in range(max(0, y0), min(size, y1)):
            for x in range(max(0, x0), min(size, x1)):
                off = (y * size + x) * 4
                if border and (x - x0 < width or y - y0 < width
                               or x1 - 1 - x < width or y1 - 1 - y < width):
                    px[off:off + 4] = bytes((*border, 255))
                else:
                    px[off:off + 4] = bytes((*color, 255))

    rect(int(size * .18), int(size * .22), int(size * .72), int(size * .64),
         back, border=accent, width=max(6, size // 90))
    rect(int(size * .28), int(size * .34), int(size * .82), int(size * .78),
         front, border=(28, 30, 36), width=max(6, size // 120))
    for i in range(3):
        y = int(size * (.44 + i * .09))
        rect(int(size * .34), y, int(size * .76), y + max(8, size // 70),
             (120, 128, 142))
    return _png(size, size, bytes(px))


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise IconError(f"{cmd[0]} failed: {proc.stderr.strip()[:400]}")
    return proc


def make_icns(png: bytes, dest: Path, tmp: Path) -> Path:
    """Build an .icns from one PNG at the largest icon size."""
    if tmp.exists():
        shutil.rmtree(tmp)
    iconset = tmp / "AppIcon.iconset"
    iconset.mkdir(parents=True)
    src = tmp / "source.png"
    src.write_bytes(png)
    for size, scale in ICONSET_SIZES:
        target = size * scale
        name = f"icon_{size}x{size}{'@2x' if scale == 2 else ''}.png"
        _run(["sips", "-z", str(target), str(target), str(src),
              "--out", str(iconset / name)])
    out = dest
    out.parent.mkdir(parents=True, exist_ok=True)
    _run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)])
    return out


def build_icon(pe_path: Path, dest_icns: Path, tmp: Path) -> dict:
    """Write dest_icns; return where the artwork came from."""
    extracted = extract_pe_icon_png(pe_path)
    if extracted is not None:
        png, size = extracted
        make_icns(png, dest_icns, tmp)
        return {"source": "pe", "width": size, "icns": str(dest_icns)}
    make_icns(default_icon_png(), dest_icns, tmp)
    return {"source": "generated", "width": 1024, "icns": str(dest_icns)}
