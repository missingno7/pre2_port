"""Object-aware inter-frame interpolation (pre2/recovered/render_interp.py).

Lerps the camera and per-sprite positions (matched by base_id) between two GameFrameSnapshots;
all other per-frame state comes from the newer frame. This is the capture-seam primitive a
future enhanced renderer uses to draw intermediate frames on its own clock.
"""
from __future__ import annotations

from pre2.recovered.render_interp import interpolate_frame
from pre2.recovered.render_model import (
    BlitMode, CameraState, GameFrameSnapshot, HudState, PaletteState, SpriteDrawCmd, TransitionCmd,
)

_MODE = next(iter(BlitMode))


def _spr(base_id, wx, wy, sx, sy):
    return SpriteDrawCmd(sprite_id=base_id, base_id=base_id, flip=False, mode=_MODE, life=0,
                         world_x=wx, world_y=wy, screen_x=sx, screen_y=sy, width=16, height=16,
                         src_seg=0, src_off=0)


def _frame(cam, sprites, **kw):
    return GameFrameSnapshot(camera=CameraState(x_px=cam[0], y_px=cam[1], cam_tile_x=0,
                             cam_tile_y=0, fine_scroll=0), palette=PaletteState(),
                             transition=TransitionCmd(), sprites=tuple(sprites), **kw)


def test_midpoint_lerps_camera_and_sprites():
    prev = _frame((0, 0), [_spr(5, 100, 200, 50, 60)])
    cur = _frame((16, 8), [_spr(5, 120, 220, 70, 80)])
    mid = interpolate_frame(prev, cur, 0.5)
    assert (mid.camera.x_px, mid.camera.y_px) == (8, 4)
    s = mid.sprites[0]
    assert (s.world_x, s.world_y, s.screen_x, s.screen_y) == (110, 210, 60, 70)


def test_endpoints():
    prev = _frame((0, 0), [_spr(5, 100, 200, 50, 60)])
    cur = _frame((16, 8), [_spr(5, 120, 220, 70, 80)])
    assert interpolate_frame(prev, cur, 1.0) is cur          # t>=1 -> newer frame verbatim
    assert interpolate_frame(None, cur, 0.5) is cur          # no prev -> newer frame
    # t=0 -> older positions (state still from cur)
    z = interpolate_frame(prev, cur, 0.0)
    assert (z.camera.x_px, z.sprites[0].screen_x) == (0, 50)


def test_new_and_despawned_sprites():
    prev = _frame((0, 0), [_spr(5, 100, 100, 10, 10), _spr(9, 0, 0, 0, 0)])      # 9 despawns
    cur = _frame((0, 0), [_spr(5, 200, 100, 20, 10), _spr(7, 300, 300, 99, 99)])  # 7 spawns
    mid = interpolate_frame(prev, cur, 0.5)
    ids = {s.base_id for s in mid.sprites}
    assert ids == {5, 7}                                    # despawned dropped, spawned kept
    spr7 = next(s for s in mid.sprites if s.base_id == 7)
    assert (spr7.screen_x, spr7.screen_y) == (99, 99)       # new sprite stays at its cur position
    spr5 = next(s for s in mid.sprites if s.base_id == 5)
    assert spr5.screen_x == 15                              # matched -> lerped


def test_frame_state_comes_from_cur():
    prev = _frame((0, 0), [], hud_state=HudState(score=100, lives=3, energy=2))
    cur = _frame((10, 0), [], hud_state=HudState(score=500, lives=2, energy=1))
    mid = interpolate_frame(prev, cur, 0.5)
    assert mid.hud_state == cur.hud_state                   # values snap to the newer frame
