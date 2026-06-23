"""HUD chrome island: the static status-bar panel + glyph font come from the persistent
``ALLFONTS.SQZ`` asset (not a transient in-VM blob), and drawing the static chrome plus the
dynamic overlay (:func:`draw_hud`) on a CLEAN framebuffer reproduces the VM HUD strip byte-exact.

Golden: the HUD strip (4 planes x 0x398, rows 176-198) off gameplay snapshot 185902's page, with
its matching HudState(score=5300, lives=2, energy=3). Skipped when the original assets are absent.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from pre2.bridge.hud_chrome import load_hud_chrome, _BG_LEN, _BG_OFF  # noqa: E402
from pre2.codecs.sqz import unpack_sqz  # noqa: E402
from pre2.recovered.hud import (  # noqa: E402
    HUD_BAR_DI, HUD_BAR_PLANE_BYTES, draw_hud, draw_status_bar,
)
from pre2.recovered.render_model import HudState  # noqa: E402
from dos_re.memory import EGA_PLANE_STRIDE  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")
FIXTURE = os.path.join(ROOT, "tests", "fixtures", "hud", "hud_strip_185902.bin")
_ALLFONTS = os.path.join(ASSETS, "ALLFONTS.SQZ")

pytestmark = pytest.mark.skipif(
    not (os.path.isfile(_ALLFONTS) and os.path.isfile(FIXTURE)),
    reason="original PRE2 assets not present",
)

# the HudState captured on snapshot 185902 alongside the golden strip
_HUD_185902 = HudState(score=5300, lives=2, energy=3)


def _strip(planes):
    """The HUD strip (di HUD_BAR_DI .. +0x398) of each plane, concatenated — page 0."""
    return b"".join(bytes(planes[p][HUD_BAR_DI:HUD_BAR_DI + HUD_BAR_PLANE_BYTES]) for p in range(4))


def test_panel_sits_right_before_the_font_glyphs():
    """The decoded ALLFONTS layout: panel bitmap [0x7B0 .. 0x1610) then the glyph font at 0x1610."""
    dec = unpack_sqz(open(_ALLFONTS, "rb").read())
    assert _BG_OFF == 0x07B0 and _BG_LEN == 0x0E60
    assert _BG_OFF + _BG_LEN == 0x1610          # the panel ends exactly where the font begins
    chrome = load_hud_chrome(ASSETS)
    assert chrome.bar == dec[_BG_OFF:_BG_OFF + _BG_LEN]
    assert len(chrome.bar) == 4 * HUD_BAR_PLANE_BYTES


def test_clean_framebuffer_hud_matches_vm_strip_byte_exact():
    """Static chrome (ALLFONTS panel) + dynamic overlay on a clean framebuffer == the VM HUD strip."""
    chrome = load_hud_chrome(ASSETS)
    planes = [bytearray(EGA_PLANE_STRIDE) for _ in range(4)]
    draw_status_bar(planes, 0, chrome.bar)
    draw_hud(planes, _HUD_185902, chrome.font, 0)
    assert _strip(planes) == open(FIXTURE, "rb").read()
