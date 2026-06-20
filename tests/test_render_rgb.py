"""The SDL viewer's NumPy decoders must be pixel-identical to the reference
``render_*_ppm`` decoders in ``scripts/render_frame.py``.  NumPy is imported inside
the test so the dependency-free test runner skips gracefully when it is absent.

PRE2 uses VGA DAC palettes, a 16-colour planar graphics path, and BIOS text, so
these cover the linear VGA decoder, the planar decoder, and the text-cell
renderer.
"""
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from dos_re.memory import Memory


def _ppm_pixels(np, result):
    _, _, ppm = result
    body = ppm.split(b"255\n", 1)[1]
    return np.frombuffer(body, dtype=np.uint8).reshape(200, 320, 3)


def test_vga_decoder_matches_ppm_renderer():
    try:
        import numpy as np
    except Exception:  # pragma: no cover - numpy optional for core tests
        print("SKIP test_vga_decoder_matches_ppm_renderer: numpy not installed")
        return

    from render_frame import render_vga_ppm
    from sdl_view import render_vga_rgb

    rnd = random.Random(1234)
    mem = Memory()
    data = mem.data
    for a in range(0xA0000, 0xA0000 + 320 * 200):
        data[a] = rnd.randrange(256)
    mb = bytes(data)

    assert np.array_equal(
        _ppm_pixels(np, render_vga_ppm(mb, 0xA000, 1)),
        render_vga_rgb(mb),
    ), "VGA mode 13h mismatch"


def test_planar_decoder_matches_ppm_renderer():
    try:
        import numpy as np
    except Exception:  # pragma: no cover - numpy optional for core tests
        print("SKIP test_planar_decoder_matches_ppm_renderer: numpy not installed")
        return

    from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
    from render_frame import render_planar_ppm
    from sdl_view import render_planar_rgb

    rnd = random.Random(5678)
    mem = Memory()
    for plane in range(4):
        base = EGA_APERTURE + EGA_PLANE_STRIDE * plane
        for off in range(0x10000):
            mem.data[base + off] = rnd.randrange(256)
    mb = bytes(mem.data)
    display_start = 0x1234

    assert np.array_equal(
        _ppm_pixels(np, render_planar_ppm(mb, display_start, 1)),
        render_planar_rgb(mb, display_start),
    ), "EGA/VGA planar mismatch"


def test_text_mode_renderer_uses_crisp_bitmap_cells():
    try:
        import numpy as np
    except Exception:  # pragma: no cover - numpy optional for core tests
        print("SKIP test_text_mode_renderer_uses_crisp_bitmap_cells: numpy not installed")
        return

    from sdl_view import render_text_rgb, _TEXT_PALETTE

    mem = bytearray(1024 * 1024)
    off = 0xB8000
    mem[off] = ord('A')
    mem[off + 1] = 0x1E  # yellow on blue

    rgb = render_text_rgb(bytes(mem), mode=3, page=0)

    assert rgb.shape == (400, 640, 3)
    assert tuple(int(v) for v in rgb[0, 0]) == _TEXT_PALETTE[1]
    # The built-in 5x7 bitmap for 'A' starts with 01110, centered at x=1 and
    # doubled vertically into an 8x16 text cell, so this pixel is foreground.
    assert tuple(int(v) for v in rgb[1, 2]) == _TEXT_PALETTE[0x0E]
    assert np.any(rgb[:16, :8] == _TEXT_PALETTE[0x0E])
