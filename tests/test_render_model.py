"""The semantic render model (pre2/recovered/render_model.py) + the sprite-intent lift.

`plan_sprite_command` exposes a sprite's render *intent* (identity / world+screen position /
graphic / blink mode) decoupled from the planar `SpriteDraw`. This guards that the semantic
command stays consistent with the faithful raster (`plan_sprite`) — same cull decision, same
sub-byte placement, same identity — reusing the object-render golden (ASM-captured, snapshot
185902). The pixels themselves are verified by tests/test_object_render.py.
"""
from __future__ import annotations

import json
from pathlib import Path

from pre2.recovered.object_render import (
    Camera, Sprite, SpriteAttr, plan_sprite, plan_sprite_command,
)
from pre2.recovered.render_model import BlitMode, SpriteDrawCmd

_FIXTURE = Path(__file__).parent / "fixtures" / "object_render_golden.json"


def test_sprite_command_consistent_with_faithful_raster():
    data = json.loads(_FIXTURE.read_text())
    assert data["sprites"], "empty golden fixture"
    checked = 0
    for it in data["sprites"]:
        spr = Sprite(**it["spr"])
        attr = SpriteAttr(**it["attr"])
        cam = Camera(**it["cam"])
        draw = plan_sprite(spr, attr, cam)
        cmd = plan_sprite_command(spr, attr, cam)

        # same cull decision as the faithful raster
        assert (cmd is None) == (draw is None)
        if cmd is None:
            continue
        checked += 1
        assert isinstance(cmd, SpriteDrawCmd)
        # identity + intent are exposed semantically
        assert cmd.sprite_id == spr.sprite_id
        assert cmd.base_id == (spr.sprite_id & 0x1FFF)
        assert cmd.world_x == spr.x and cmd.world_y == spr.y
        assert cmd.flip == draw.flipped
        assert int(cmd.mode) == draw.mode and isinstance(cmd.mode, BlitMode)
        assert cmd.src_seg == attr.src_seg and cmd.src_off == attr.src_off
        # screen placement consistent with the faithful raster's sub-byte shift
        assert (cmd.screen_x & 7) == draw.shift
        # the command is pure semantic data — no VRAM offsets leak in
        assert not hasattr(cmd, "dest_off") and not hasattr(cmd, "byte_width")
    assert checked > 0, "fixture drew no sprites"


def test_sprite_command_culls_empty_slot():
    cam = Camera(cam_x=0, cam_y=0, fine_scroll=0, row_factor=0, dest_page=0,
                 row_stride=40, global_shift=0, frame=0)
    attr = SpriteAttr(width=8, height=8, x_off=0, y_off=0, src_seg=0, src_off=0)
    empty = Sprite(x=0, y=0, sprite_id=0xFFFF, flags=0, life=0)
    assert plan_sprite_command(empty, attr, cam) is None
