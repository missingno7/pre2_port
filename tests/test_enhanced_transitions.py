"""Enhanced transition projections — geometry tests (the byte-exact-vs-faithful equivalence is proven by
``pre2/probes/verify_enhanced_vfade.py`` against compose_vfade_planes; these cover the pure RGB geometry)."""
from __future__ import annotations

import numpy as np

from pre2.enhanced.transitions import apply_vfade


def test_apply_vfade_blacks_converging_bands():
    f = np.full((200, 320, 3), 200, np.uint8)
    apply_vfade(f, 40, 120)
    assert (f[:40] == 0).all(), "top band not blacked"
    assert (f[120:176] == 0).all(), "bottom band not blacked"
    assert (f[40:120] == 200).all(), "middle (uncleared) must be untouched"
    assert (f[176:] == 200).all(), "HUD band must be untouched (vfade is viewport-only)"


def test_apply_vfade_inactive_is_noop():
    f = np.full((200, 320, 3), 123, np.uint8)
    apply_vfade(f, 0, 176)             # top=0, bot=176 -> nothing cleared == a normal frame
    assert (f == 123).all()


def test_apply_vfade_fully_closed_blacks_whole_viewport():
    f = np.full((200, 320, 3), 200, np.uint8)
    apply_vfade(f, 88, 88)             # bands meet in the middle
    assert (f[:176] == 0).all()
    assert (f[176:] == 200).all()


def test_apply_vfade_clamps_out_of_range():
    f = np.full((200, 320, 3), 50, np.uint8)
    apply_vfade(f, -5, 999)            # clamps to [0,176]; bot>=176 -> no bottom band, top<=0 -> no top band
    assert (f == 50).all()
