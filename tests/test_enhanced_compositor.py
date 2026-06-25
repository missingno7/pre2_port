"""Modern enhanced object compositor — logic tests (synthetic, snapshot-free).

The witness alpha=1 PARITY (composite == faithful for spiders / player-death / gameplay / boss) is proven by
``pre2/probes/verify_enhanced_parity.py`` (snapshot-based; snapshots are gitignored). These committed tests
cover the compositor's interpolation + blit logic: SLOT-based identity (stable across the walk/blink animation
that changes sprite_id/base_id every frame), WORLD-position interpolation (so the per-animation-frame screen
offset does NOT inject shake), alpha endpoints, fixed HUD sprites, draw order, clipping.
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


def _spr(slot, x, y, rgba, *, world=None, handle=None, sprite_id=0x100, interpolate=True):
    wx, wy = world if world is not None else (x, y)
    return SpriteInstance(handle=handle if handle is not None else slot, slot=slot,
                          base_id=sprite_id & 0x1FFF, sprite_id=sprite_id,
                          world_x=wx, world_y=wy, screen_x=x, screen_y=y,
                          tex_off_x=0, tex_off_y=0, rgba=rgba, interpolate=interpolate)


def _frame(sprites, bg=None):
    bg = np.zeros((16, 32, 3), np.uint8) if bg is None else bg
    return EnhancedFrameState(background_rgb=bg, camera=(0, 0), sprites=sprites,
                              faithful_rgb=bg, unsupported=[])


def test_alpha1_places_sprite_at_current_position():
    out = compose(_frame([_spr(1, 5, 4, _solid(3, 2, (10, 20, 30)))]), None, 1.0)
    assert tuple(out[4, 5]) == (10, 20, 30) and tuple(out[5, 7]) == (10, 20, 30)
    assert tuple(out[0, 0]) == (0, 0, 0)


def test_world_interpolation_lerps_matched_slot():
    prev = _frame([_spr(7, 0, 0, _solid(2, 2, (9, 9, 9)))])
    cur = _frame([_spr(7, 10, 0, _solid(2, 2, (9, 9, 9)))])
    assert tuple(compose(cur, prev, 0.5)[0, 5]) == (9, 9, 9)        # midpoint of the world move
    assert tuple(compose(cur, prev, 0.0)[0, 0]) == (9, 9, 9)        # alpha=0 -> prev world pos
    assert tuple(compose(cur, prev, 1.0)[0, 10]) == (9, 9, 9)       # alpha=1 -> cur


def test_animation_changes_id_AND_slot_but_handle_is_stable_so_still_interpolates():
    # sprite_id/base_id change every source frame (0x213a->0x213b) AND the slot shifts on spawn (104->103),
    # but the persistent handle is stable -> match + interpolate.
    prev = _frame([_spr(104, 0, 0, _solid(2, 2, (5, 6, 7)), handle=0xb96c, sprite_id=0x213a)])
    cur = _frame([_spr(103, 10, 0, _solid(2, 2, (5, 6, 7)), handle=0xb96c, sprite_id=0x213b)])
    out = compose(cur, prev, 0.5)
    assert tuple(out[0, 5]) == (5, 6, 7) and tuple(out[0, 0]) == (0, 0, 0) and tuple(out[0, 10]) == (0, 0, 0)


def test_handle_reuse_large_jump_snaps_to_current_no_teleport_interp():
    # A handle reused for a different object (despawn+spawn) shows a large world jump -> must NOT interpolate
    # across it (no teleport smear); snap to the current position.
    prev = _frame([_spr(5, 0, 0, _solid(2, 2, (8, 8, 8)), handle=0x1234, world=(0, 0))])
    cur = _frame([_spr(5, 28, 0, _solid(2, 2, (8, 8, 8)), handle=0x1234, world=(200, 0))])  # +200 > gate
    out = compose(cur, prev, 0.5)
    assert tuple(out[0, 28]) == (8, 8, 8), "large-jump (reuse) object should snap to current"
    assert tuple(out[0, 14]) == (0, 0, 0), "must not interpolate across a handle-reuse teleport"


def test_still_object_with_oscillating_screen_offset_does_not_drift():
    # The shake bug: world position is constant, but the per-animation-frame draw offset makes screen_x
    # oscillate (5 -> 6). Interpolating SCREEN would drift (5.5); interpolating WORLD (delta 0) must NOT.
    prev = _frame([_spr(2, 5, 5, _solid(2, 2, (1, 2, 3)), world=(50, 50))])
    cur = _frame([_spr(2, 6, 5, _solid(2, 2, (1, 2, 3)), world=(50, 50))])
    for a in (0.0, 0.3, 0.7, 1.0):
        out = compose(cur, prev, a)
        assert tuple(out[5, 6]) == (1, 2, 3), f"still object drifted at alpha={a}"
        assert tuple(out[5, 5]) == (0, 0, 0), f"still object shows sub-frame drift at alpha={a}"


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
