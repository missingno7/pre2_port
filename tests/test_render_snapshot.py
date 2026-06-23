"""The GameFrameSnapshot assembler (pre2/recovered/render_snapshot.py).

`plan_tiles` enumerates the background grid as semantic TileDrawCmds; `build_frame_snapshot`
assembles the full per-frame render intent (camera + palette + sprites + tiles). The tile
enumeration is cross-checked in-VM against the recovered (ASM-matched) `redraw_animated_grid`
tile walk on gameplay snapshot 185902 (pre2/probes — 240/240 match); this is the fast
committed unit guard on the indexing + screen mapping.
"""
from __future__ import annotations

from types import SimpleNamespace

from pre2.recovered.animation import AnimStep
from pre2.recovered.render_frame import IrisState
from pre2.recovered.render_model import GameFrameSnapshot, TileDrawCmd, TransitionKind
from pre2.recovered.render_snapshot import (
    TILEMAP_STRIDE, VISIBLE_COLS, VISIBLE_ROWS, build_frame_snapshot, plan_tiles,
)


def test_plan_tiles_indexing_and_positions():
    cam_x, cam_y = 5, 3
    tiles = bytearray(0x10000)
    for r in range(VISIBLE_ROWS):
        for c in range(VISIBLE_COLS):
            tiles[((cam_y + r) * TILEMAP_STRIDE + cam_x + c) & 0xFFFF] = (r * 20 + c) & 0xFF
    st = SimpleNamespace(tiles=bytes(tiles), blit_type=bytes(256),
                         camera_x=cam_x, camera_y=cam_y)

    cmds = plan_tiles(st)
    assert len(cmds) == VISIBLE_COLS * VISIBLE_ROWS == 240
    for r in range(VISIBLE_ROWS):
        for c in range(VISIBLE_COLS):
            cmd = cmds[r * VISIBLE_COLS + c]
            assert isinstance(cmd, TileDrawCmd)
            assert cmd.tile_id == (r * 20 + c) & 0xFF       # the right tile-map cell
            assert (cmd.grid_col, cmd.grid_row) == (c, r)
            assert (cmd.screen_x, cmd.screen_y) == (c * 16, r * 16)


def test_build_frame_snapshot_structure():
    st = SimpleNamespace(
        tiles=bytes(0x10000), blit_type=bytes(256), camera_x=10, camera_y=4,
        fine_scroll=3, fade=None, object_camera=None, object_sprites=(), object_attrs={},
    )
    snap = build_frame_snapshot(st)
    assert isinstance(snap, GameFrameSnapshot)
    assert snap.camera.x_px == 10 * 16
    assert snap.camera.y_px == 4 * 16 + 3       # vertical fine scroll folded into the camera
    assert snap.camera.cam_tile_x == 10 and snap.camera.fine_scroll == 3
    assert len(snap.tiles) == 240
    assert snap.sprites == () and snap.hud == ()   # no object pass
    assert snap.palette.fade_amount == 0
    assert snap.phase == "gameplay"
    assert snap.transition.kind == TransitionKind.NONE   # no iris -> NONE
    assert snap.animation.active is False                 # no anim -> default (frozen cycle)


def _state(**kw):
    base = dict(tiles=bytes(0x10000), blit_type=bytes(256), camera_x=0, camera_y=0,
                fine_scroll=0, fade=None, palette=None, iris=None, anim=None, shake=None,
                object_camera=None, object_sprites=(), object_attrs={})
    base.update(kw)
    return SimpleNamespace(**base)


def test_camera_shake_state():
    from pre2.recovered.render_model import CameraShakeState
    # applied_offset = the confirmed [0x6BF8] row_factor jolt (magnitude on odd parity)
    sh = CameraShakeState(magnitude=7, active=True, phase=1, applied_offset=7)
    snap = build_frame_snapshot(_state(shake=sh))
    assert snap.shake is sh and snap.shake.magnitude == 7 and snap.shake.active
    assert snap.shake.applied_offset == 7
    # no shake captured -> inactive default (frozen state, no offset)
    s = build_frame_snapshot(_state(shake=None)).shake
    assert s.magnitude == 0 and s.active is False and s.applied_offset is None


def test_palette_full_state_passthrough():
    from pre2.recovered.render_model import FadePhase, PaletteState
    full = PaletteState(colors=tuple([(1, 2, 3)] * 16), base_index=2, phase=FadePhase.OUT,
                        fade_amount=12, fade_from=b"\x01" * 48, fade_to=b"\x02" * 48)
    # the full palette state machine (displayed colours + IN/OUT phase) flows through verbatim
    assert build_frame_snapshot(_state(palette=full)).palette is full
    # fallback: no captured palette -> empty colours, NONE phase (fade-only / no displayed colours)
    p = build_frame_snapshot(_state(palette=None, fade=None)).palette
    assert p.colors == () and p.phase == FadePhase.NONE


def test_animation_state():
    snap = build_frame_snapshot(_state(anim=AnimStep(frame_ptr=0x6788, throttle=0x04,
                                                     active=True, speed=0)))
    a = snap.animation
    assert (a.frame_index, a.frame_count) == (1, 3)   # 0x6788 = 2nd of 3 cycle frames
    assert a.frame_ptr == 0x6788 and a.throttle_counter == 0x04
    assert a.throttle_period == 4 and a.active is True
    # fast scroll halves the period
    assert build_frame_snapshot(_state(anim=AnimStep(0x6688, 0, True, 0x14))).animation.throttle_period == 2
    # no anim -> default cycle metadata
    assert build_frame_snapshot(_state(anim=None)).animation.frame_count == 3


def test_transition_iris_state():
    # iris running -> IRIS command carrying centre + shrinking radius (as render state)
    snap = build_frame_snapshot(_state(iris=IrisState(radius=0xE6, center_x=68, center_y=275)))
    t = snap.transition
    assert t.kind == TransitionKind.IRIS
    assert (t.center_x, t.center_y, t.radius) == (68, 275, 0xE6)
    assert t.fade_amount == 0       # geometric transition; the palette fade lives in PaletteState

    # no iris -> NONE (and a missing attr is tolerated, like the structure test)
    assert build_frame_snapshot(_state(iris=None)).transition.kind == TransitionKind.NONE
