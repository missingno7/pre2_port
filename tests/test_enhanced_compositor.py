"""Modern enhanced object compositor — logic tests (synthetic, snapshot-free).

The witness alpha=1 PARITY (composite == faithful for spiders / player-death / gameplay / boss) is proven by
``pre2/probes/verify_enhanced_parity.py`` (snapshot-based; snapshots are gitignored). These committed tests
cover the compositor's interpolation + blit logic: alpha endpoints, object-aware base_id interpolation,
fixed (non-interpolated) HUD sprites, draw order, clipping, and new/despawned sprites.
"""
from __future__ import annotations

import numpy as np

from pre2.enhanced.compositor import compose
from pre2.enhanced.frame_state import EnhancedFrameState, SpriteInstance


def _solid(w, h, rgb):
    s = np.zeros((h, w, 4), np.uint8)
    s[..., :3] = rgb
    s[..., 3] = 255
    return s


def _frame(sprites, bg=None):
    bg = np.zeros((16, 32, 3), np.uint8) if bg is None else bg
    return EnhancedFrameState(background_rgb=bg, camera=(0, 0), sprites=sprites,
                              faithful_rgb=bg, unsupported=[])


def test_alpha1_places_sprite_at_current_anchor():
    cur = _frame([SpriteInstance(1, 1, 5, 4, _solid(3, 2, (10, 20, 30)))])
    out = compose(cur, None, 1.0)
    assert tuple(out[4, 5]) == (10, 20, 30) and tuple(out[5, 7]) == (10, 20, 30)
    assert tuple(out[0, 0]) == (0, 0, 0)               # background elsewhere


def test_object_aware_interpolation_lerps_matched_base_id():
    prev = _frame([SpriteInstance(7, 7, 0, 0, _solid(2, 2, (9, 9, 9)))])
    cur = _frame([SpriteInstance(7, 7, 10, 0, _solid(2, 2, (9, 9, 9)))])
    mid = compose(cur, prev, 0.5)
    assert tuple(mid[0, 5]) == (9, 9, 9)               # lerped to x=5
    assert tuple(mid[0, 0]) == (0, 0, 0) and tuple(mid[0, 10]) == (0, 0, 0)
    assert tuple(compose(cur, prev, 0.0)[0, 0]) == (9, 9, 9)    # alpha=0 -> prev
    assert tuple(compose(cur, prev, 1.0)[0, 10]) == (9, 9, 9)   # alpha=1 -> cur


def test_fixed_hud_sprite_is_not_interpolated():
    prev = _frame([SpriteInstance(0x135, 0x135, 0, 0, _solid(2, 2, (1, 2, 3)), interpolate=False)])
    cur = _frame([SpriteInstance(0x135, 0x135, 10, 0, _solid(2, 2, (1, 2, 3)), interpolate=False)])
    out = compose(cur, prev, 0.5)
    assert tuple(out[0, 10]) == (1, 2, 3)              # stays at cur anchor
    assert tuple(out[0, 5]) == (0, 0, 0)


def test_new_sprite_without_prev_match_uses_current_position():
    prev = _frame([])
    cur = _frame([SpriteInstance(3, 3, 8, 0, _solid(2, 2, (4, 5, 6)))])
    assert tuple(compose(cur, prev, 0.5)[0, 8]) == (4, 5, 6)


def test_draw_order_back_to_front():
    cur = _frame([SpriteInstance(1, 1, 0, 0, _solid(4, 4, (100, 0, 0))),
                  SpriteInstance(2, 2, 0, 0, _solid(2, 2, (0, 200, 0)))])
    out = compose(cur, None, 1.0)
    assert tuple(out[0, 0]) == (0, 200, 0)             # later sprite wins the overlap
    assert tuple(out[3, 3]) == (100, 0, 0)             # earlier sprite where not overlapped


def test_clipping_offscreen_does_not_crash_and_clips():
    cur = _frame([SpriteInstance(1, 1, -1, -1, _solid(3, 3, (50, 60, 70)))])
    out = compose(cur, None, 1.0)
    assert tuple(out[0, 0]) == (50, 60, 70)            # visible part drawn
    assert out.shape == (16, 32, 3)


def test_transparent_alpha_shows_background():
    spr = _solid(3, 3, (77, 77, 77))
    spr[1, 1, 3] = 0                                   # hole in the middle
    bg = np.full((8, 8, 3), (5, 5, 5), np.uint8)
    out = compose(_frame([SpriteInstance(1, 1, 2, 2, spr)], bg=bg), None, 1.0)
    assert tuple(out[3, 3]) == (5, 5, 5)               # background shows through the hole
    assert tuple(out[2, 2]) == (77, 77, 77)
