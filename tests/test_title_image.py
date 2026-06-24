"""Tests for the recovered title 13h image (pre2.recovered.title_image).

The byte-exact-vs-ASM proof is the live probe (pre2/probes/verify_title_image.py: render_title_image of
the decoded PRESENT.SQZ == the displayed title framebuffer Δ=0). These lock the layer offsets/lengths and
the compose (logo-top overlays the top of the background)."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pre2.recovered.title_image import (  # noqa: E402
    render_title_image, title_background, title_logo_top, title_palette)


def _asset():
    # palette (0x300 of 0x11) + background (64000 of 0x22) + gap + logo-top (29760 of 0x33) at 0x10300
    a = bytearray(0x10300 + 29760)
    a[0:0x300] = bytes([0x11]) * 0x300
    a[0x300:0x300 + 64000] = bytes([0x22]) * 64000
    a[0x10300:0x10300 + 29760] = bytes([0x33]) * 29760
    return bytes(a)


def test_layer_slices():
    a = _asset()
    assert title_palette(a) == bytes([0x11]) * 0x300
    assert title_background(a) == bytes([0x22]) * 64000
    assert title_logo_top(a) == bytes([0x33]) * 29760


def test_compose_overlay_top_only():
    a = _asset()
    img = render_title_image(a)
    assert len(img) == 64000
    # top 29760 bytes (rows 0-92) = the logo-top
    assert all(b == 0x33 for b in img[:29760])
    # below the logo-top = the background
    assert all(b == 0x22 for b in img[29760:])


def test_logo_top_is_320x93():
    a = _asset()
    assert len(title_logo_top(a)) == 93 * 320
