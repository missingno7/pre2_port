"""Enhanced presentation v1 — scroll-motion interpolation (pre2/enhanced/present.py)."""
from __future__ import annotations

import numpy as np

from pre2.enhanced.present import scroll_subframes, shift_viewport


def _frame():
    # distinct gameplay rows + a constant HUD band so we can check the HUD never moves
    rgb = np.zeros((200, 320, 3), dtype=np.uint8)
    rgb[:184, :, 0] = np.arange(184)[:, None]              # gameplay: row index in R
    rgb[:184, :, 2] = (np.arange(320) % 256)[None, :]      # gameplay: col index in B (varies in x)
    rgb[184:, :, 1] = 99                                   # HUD band: constant green
    return rgb


def test_shift_keeps_hud_fixed():
    rgb = _frame()
    out = shift_viewport(rgb, dx=0, dy=3, hud_top=184)
    assert np.array_equal(out[184:], rgb[184:])            # HUD untouched
    # gameplay shifted down by 3: row 10 of output came from row 7 of input
    assert np.array_equal(out[10], rgb[7])
    assert (out[:184] != rgb[:184]).any()                  # gameplay did move


def test_shift_zero_is_identity():
    rgb = _frame()
    assert np.array_equal(shift_viewport(rgb, 0, 0), rgb)


def test_scroll_subframes_endpoint_is_cur():
    rgb = _frame()
    frames = scroll_subframes((0, 0), (8, 0), rgb, steps=4)
    assert len(frames) == 4
    assert np.array_equal(frames[-1], rgb)                 # last subframe = cur unshifted
    # earlier subframes are shifted (viewport sits back toward the previous camera)
    assert not np.array_equal(frames[0], rgb)
    # all subframes keep the HUD fixed
    for f in frames:
        assert np.array_equal(f[184:], rgb[184:])
