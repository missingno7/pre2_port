"""The DETACHABLE bridge for the boot BITMAP fossils — decode them to/from PNG.

The boot data's structured tables become readable value constants (pre2/native/boot_tables.py); the boot GRAPHICS
regions (font glyphs, UI/sprite tiles embedded in the DOS DATA segment) are images, so their readable/editable
form is a PNG. This module renders a boot byte-region as a lossless 1bpp 8x8-tile sprite sheet and reads it back
to the exact bytes — so the artwork can live as an editable PNG asset, and the bridge re-encodes it to the DOS
byte layout for byte-exact verification. Detach the bridge and the game just ships the PNGs.

1bpp packing: each 8-byte run is one 8x8 tile (byte = a row, bit7 = leftmost pixel); tiles fill a grid
``TILES_WIDE`` across. The round-trip is exact because bit<->pixel is a bijection (trailing pad bytes are
dropped on import via the recorded original length).
"""
from __future__ import annotations

from pathlib import Path

_FG = (233, 233, 233)
_BG = (18, 18, 18)


def region_to_png(data: bytes, path: str, tile_w: int = 8, tile_h: int = 8, tiles_wide: int = 32,
                  gap: int = 1) -> tuple[int, int]:
    """Render ``data`` as a lossless 1bpp tile-sheet PNG: each ``tile_w*tile_h/8``-byte run is one tile
    (row-major, bit7 = leftmost pixel), ``tiles_wide`` tiles across, ``gap`` px between tiles for legibility.
    Returns (width, height). Two boot formats are known: 8x8 font glyphs and 16x32 sprite blocks."""
    from PIL import Image
    tb = tile_w * tile_h // 8
    stride = tile_w // 8
    n_tiles = (len(data) + tb - 1) // tb
    rows = (n_tiles + tiles_wide - 1) // tiles_wide
    w = tiles_wide * (tile_w + gap) - gap
    h = rows * (tile_h + gap) - gap
    img = Image.new("RGB", (w, h), _BG)
    px = img.load()
    for t in range(n_tiles):
        ox, oy = (t % tiles_wide) * (tile_w + gap), (t // tiles_wide) * (tile_h + gap)
        for r in range(tile_h):
            for cb in range(stride):
                b = data[t * tb + r * stride + cb] if t * tb + r * stride + cb < len(data) else 0
                for x in range(8):
                    if (b >> (7 - x)) & 1:
                        px[ox + cb * 8 + x, oy + r] = _FG
    img.save(path)
    return w, h


def png_to_region(path: str, length: int, tile_w: int = 8, tile_h: int = 8, tiles_wide: int = 32,
                  gap: int = 1) -> bytes:
    """Inverse of :func:`region_to_png` — read a sheet back to exactly ``length`` bytes."""
    from PIL import Image
    px = Image.open(path).convert("RGB").load()
    tb = tile_w * tile_h // 8
    stride = tile_w // 8
    n_tiles = (length + tb - 1) // tb
    out = bytearray()
    for t in range(n_tiles):
        ox, oy = (t % tiles_wide) * (tile_w + gap), (t // tiles_wide) * (tile_h + gap)
        for r in range(tile_h):
            for cb in range(stride):
                b = 0
                for x in range(8):
                    if px[ox + cb * 8 + x, oy + r] != _BG:
                        b |= 1 << (7 - x)
                out.append(b)
    return bytes(out[:length])


def verify_boot_graphics_roundtrip(tmp_path: str | None = None) -> None:
    """Prove a boot graphics region survives bytes -> PNG -> bytes unchanged."""
    import tempfile
    from pre2.native.boot_manifest import BOOT_REGIONS, _dgroup
    dg = _dgroup()
    gfx = next(r for r in BOOT_REGIONS if r.kind == "graphics")
    data = bytes(dg[gfx.lo:gfx.hi + 1])
    p = tmp_path or str(Path(tempfile.gettempdir()) / "boot_gfx_roundtrip.png")
    region_to_png(data, p)
    back = png_to_region(p, len(data))
    if back != data:
        off = next(k for k in range(len(data)) if back[k] != data[k])
        raise AssertionError(f"boot graphics PNG round-trip diverged at region byte 0x{off:04X}")


if __name__ == "__main__":
    from pre2.native.boot_manifest import BOOT_REGIONS, _dgroup
    dg = _dgroup()
    gfx = next(r for r in BOOT_REGIONS if r.kind == "graphics")
    data = bytes(dg[gfx.lo:gfx.hi + 1])
    w, h = region_to_png(data, "artifacts/boot_graphics.png")
    verify_boot_graphics_roundtrip("artifacts/boot_graphics.png")
    print(f"boot graphics -> artifacts/boot_graphics.png ({w}x{h}px, {len(data)} bytes) — round-trip byte-exact")
