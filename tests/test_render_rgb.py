"""The SDL viewer's NumPy decoders must be pixel-identical to the reference
``render_*_ppm`` decoders in ``scripts/render_frame.py``.  NumPy is imported inside
the test so the dependency-free test runner skips gracefully when it is absent.
"""
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (ROOT, ROOT / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from dos_re.memory import Memory, EGA_APERTURE, EGA_SHADOW_SIZE


def _ppm_pixels(np, result):
    _, _, ppm = result
    body = ppm.split(b"255\n", 1)[1]
    return np.frombuffer(body, dtype=np.uint8).reshape(200, 320, 3)


def test_numpy_decoders_match_ppm_renderers():
    try:
        import numpy as np
    except Exception:  # pragma: no cover - numpy optional for core tests
        print("SKIP test_numpy_decoders_match_ppm_renderers: numpy not installed")
        return

    from render_frame import render_ppm, render_tandy_ppm, render_ega_ppm
    from sdl_view import render_cga_rgb, render_tandy_rgb, render_ega_rgb

    rnd = random.Random(1234)
    mem = Memory()
    data = mem.data
    for a in range(0xB8000, 0xB8000 + 0x8000):
        data[a] = rnd.randrange(256)
    for plane in range(4):
        base = EGA_APERTURE + plane * 0x10000
        for a in range(base, base + 0x2000):
            data[a] = rnd.randrange(256)
    mb = bytes(data)

    for palette in ("1h", "1l", "0h", "0l"):
        assert np.array_equal(
            _ppm_pixels(np, render_ppm(mb, 0xB800, palette, 1)),
            render_cga_rgb(mb, palette),
        ), f"CGA palette {palette} mismatch"

    assert np.array_equal(
        _ppm_pixels(np, render_tandy_ppm(mb, 0xB800, 1)),
        render_tandy_rgb(mb),
    ), "Tandy mismatch"

    for start in (0, 0x50, 0x1F40, 0x7D00, 0xFFE0):
        assert np.array_equal(
            _ppm_pixels(np, render_ega_ppm(mb, 0xA000, 1, start)),
            render_ega_rgb(mb, start),
        ), f"EGA full-memory start {start:#06x} mismatch"

    # The live viewer publishes only the tight 4-plane shadow slice (planes at
    # offset 0), not full memory -- both layouts must decode identically.
    tight = bytes(memoryview(data)[EGA_APERTURE:EGA_APERTURE + EGA_SHADOW_SIZE])
    for start in (0, 0x50, 0x1F40, 0xFFE0):
        assert np.array_equal(
            _ppm_pixels(np, render_ega_ppm(tight, 0xA000, 1, start)),
            render_ega_rgb(tight, start),
        ), f"EGA tight-slice start {start:#06x} mismatch"


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
