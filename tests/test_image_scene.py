"""Tests for the mode-13h IMAGE-scene bridge (pre2.bridge.image_scene).

The byte-exact-vs-ASM proof is the live wiring (the faithful 13h path renders the identified image == the
ASM A000 framebuffer Δ=0, both the title fade-in and steady phases). These lock the identification
(fingerprint the copy source) and the title logo toggle against the real PRESENT.SQZ asset."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from pre2.bridge.image_scene import identify_image, render_image_scene  # noqa: E402
from pre2.codecs.sqz import unpack_sqz  # noqa: E402
from pre2.recovered.title_image import render_title_image, title_background  # noqa: E402

_ASSETS = "assets"
_HAVE_PRESENT = os.path.exists(os.path.join(_ASSETS, "PRESENT.SQZ"))

pytestmark = pytest.mark.skipif(not _HAVE_PRESENT, reason="game assets not present")


def _present():
    with open(os.path.join(_ASSETS, "PRESENT.SQZ"), "rb") as f:
        return unpack_sqz(f.read())


def test_identify_title_by_copy_source():
    # the ASM copies the image (asset offset 0x300) to A000 -> identify from its first bytes
    dec = _present()
    src = dec[0x300:0x300 + 256]
    assert identify_image(src, _ASSETS) == "PRESENT.SQZ"


def test_identify_unknown_returns_none():
    assert identify_image(b"\x00" * 256, _ASSETS) is None


def test_render_title_with_and_without_logo():
    dec = _present()
    full = render_image_scene("PRESENT.SQZ", _ASSETS, with_logo=True)
    bg = render_image_scene("PRESENT.SQZ", _ASSETS, with_logo=False)
    assert full == render_title_image(dec)            # bg + logo-top
    assert bg == bytes(title_background(dec))          # bg only (fade-in phase)
    assert len(full) == 64000 and len(bg) == 64000
    assert full != bg                                  # the logo region differs
