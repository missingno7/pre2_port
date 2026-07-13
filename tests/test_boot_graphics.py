"""Gate: the boot bitmap fossils convert to PNG and back byte-exact (the image form of the inversion)."""
import tempfile
from pathlib import Path


def test_boot_graphics_region_png_round_trips_byte_exact():
    from pre2.bridge.boot_graphics import verify_boot_graphics_roundtrip
    verify_boot_graphics_roundtrip(str(Path(tempfile.gettempdir()) / "test_boot_gfx.png"))


def test_boot_digit_sprites_round_trip():
    from pre2.native.boot_manifest import _dgroup
    from pre2.bridge.boot_graphics import png_to_region, region_to_png
    dg = _dgroup()
    # the 0xCE8A stride-0x58 blocks hold the 16x32 HUD digit sprites (1..9)
    blocks = [bytes(dg[0xCE8A + k * 0x58:0xCE8A + k * 0x58 + 64]) for k in range(9)]
    payload = b"".join(blocks)
    p = str(Path(tempfile.gettempdir()) / "test_boot_digits.png")
    region_to_png(payload, p, tile_w=16, tile_h=32, tiles_wide=9, gap=2)
    assert png_to_region(p, len(payload), tile_w=16, tile_h=32, tiles_wide=9, gap=2) == payload
