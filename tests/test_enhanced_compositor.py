"""Modern enhanced object compositor — logic tests (synthetic, snapshot-free).

The witness alpha=1 PARITY (composite == faithful for spiders / player-death / gameplay / boss) is proven by
``pre2/probes/verify_enhanced_parity.py`` (snapshot-based; snapshots are gitignored). These committed tests
cover the compositor's interpolation + blit logic: alpha endpoints, SLOT-based identity (stable across the
walk/blink animation that changes sprite_id/base_id every frame), fixed HUD sprites, draw order, clipping.
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


def _spr(slot, x, y, rgba, *, sprite_id=0x100, interpolate=True):
    return SpriteInstance(slot=slot, base_id=sprite_id & 0x1FFF, sprite_id=sprite_id,
                          screen_x=x, screen_y=y, tex_off_x=0, tex_off_y=0,
                          rgba=rgba, interpolate=interpolate)


def _frame(sprites, bg=None):
    bg = np.zeros((16, 32, 3), np.uint8) if bg is None else bg
    return EnhancedFrameState(background_rgb=bg, camera=(0, 0), sprites=sprites,
                              faithful_rgb=bg, unsupported=[])


def test_alpha1_places_sprite_at_current_position():
    cur = _frame([_spr(1, 5, 4, _solid(3, 2, (10, 20, 30)))])
    out = compose(cur, None, 1.0)
    assert tuple(out[4, 5]) == (10, 20, 30) and tuple(out[5, 7]) == (10, 20, 30)
    assert tuple(out[0, 0]) == (0, 0, 0)


def test_interpolation_lerps_matched_slot():
    prev = _frame([_spr(7, 0, 0, _solid(2, 2, (9, 9, 9)))])
    cur = _frame([_spr(7, 10, 0, _solid(2, 2, (9, 9, 9)))])
    assert tuple(compose(cur, prev, 0.5)[0, 5]) == (9, 9, 9)        # lerped to x=5
    assert tuple(compose(cur, prev, 0.0)[0, 0]) == (9, 9, 9)        # alpha=0 -> prev
    assert tuple(compose(cur, prev, 1.0)[0, 10]) == (9, 9, 9)       # alpha=1 -> cur


def test_animation_changes_id_but_slot_is_stable_so_still_interpolates():
    # The regression we fixed: an animating object's sprite_id/base_id change EVERY source frame
    # (0x213a -> 0x213b), but its active-list slot is stable -> it must still match + interpolate.
    prev = _frame([_spr(104, 0, 0, _solid(2, 2, (5, 6, 7)), sprite_id=0x213a)])
    cur = _frame([_spr(104, 10, 0, _solid(2, 2, (5, 6, 7)), sprite_id=0x213b)])
    out = compose(cur, prev, 0.5)
    assert tuple(out[0, 5]) == (5, 6, 7), "slot-stable object did not interpolate across an animation frame"
    assert tuple(out[0, 0]) == (0, 0, 0) and tuple(out[0, 10]) == (0, 0, 0)


def test_texture_offset_is_applied_at_interpolated_anchor():
    spr = _spr(3, 4, 4, _solid(2, 2, (1, 2, 3)))
    spr.tex_off_x, spr.tex_off_y = 2, 1
    prev = _frame([_spr(3, 0, 0, _solid(2, 2, (1, 2, 3)))])
    prev.sprites[0].tex_off_x, prev.sprites[0].tex_off_y = 2, 1
    out = compose(_frame([spr]), prev, 1.0)
    assert tuple(out[5, 6]) == (1, 2, 3)              # drawn at (screen_x+off_x, screen_y+off_y)=(6,5)


def test_fixed_hud_sprite_is_not_interpolated():
    prev = _frame([_spr(0, 0, 0, _solid(2, 2, (1, 2, 3)), interpolate=False)])
    cur = _frame([_spr(0, 10, 0, _solid(2, 2, (1, 2, 3)), interpolate=False)])
    out = compose(cur, prev, 0.5)
    assert tuple(out[0, 10]) == (1, 2, 3) and tuple(out[0, 5]) == (0, 0, 0)


def test_new_slot_without_prev_match_uses_current_position():
    assert tuple(compose(_frame([_spr(3, 8, 0, _solid(2, 2, (4, 5, 6)))]), _frame([]), 0.5)[0, 8]) == (4, 5, 6)


def test_draw_order_back_to_front():
    cur = _frame([_spr(1, 0, 0, _solid(4, 4, (100, 0, 0))), _spr(2, 0, 0, _solid(2, 2, (0, 200, 0)))])
    out = compose(cur, None, 1.0)
    assert tuple(out[0, 0]) == (0, 200, 0) and tuple(out[3, 3]) == (100, 0, 0)


def test_clipping_offscreen_does_not_crash():
    out = compose(_frame([_spr(1, -1, -1, _solid(3, 3, (50, 60, 70)))]), None, 1.0)
    assert tuple(out[0, 0]) == (50, 60, 70) and out.shape == (16, 32, 3)


def test_transparent_alpha_shows_background():
    spr = _solid(3, 3, (77, 77, 77))
    spr[1, 1, 3] = 0
    bg = np.full((8, 8, 3), (5, 5, 5), np.uint8)
    out = compose(_frame([_spr(1, 2, 2, spr)], bg=bg), None, 1.0)
    assert tuple(out[3, 3]) == (5, 5, 5) and tuple(out[2, 2]) == (77, 77, 77)
