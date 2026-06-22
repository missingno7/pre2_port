"""Byte-exact regression for the recovered moving-sprite renderer (1030:26FA).

Golden fixture captured from the original ASM under the VM (snapshot 185902): for
each drawn sprite, its plan inputs (record / attributes / camera), source pixel
bytes, and the four EGA planes before and after the ASM's blit, restricted to the
sprite's written offsets. The test runs the recovered planner + blit and asserts it
reproduces the ASM's planes exactly — covering opaque/masked/shifted (0-7) and the
left/right edge-clip variants (incl. the byte_width==0 sliver).

In-VM lockstep over full gameplay lives in pre2/probes/verify_object_*.py; this is
the fast committed check. (H-flip is not yet recovered — no flipped sprites occur in
the captured scenes.)
"""
from __future__ import annotations

import json
from pathlib import Path

from pre2.recovered.object_render import (
    Camera, Sprite, SpriteAttr, SpriteDraw, paint_sprite, plan_sprite,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "object_render_golden.json"


def test_object_render_byte_exact_vs_asm():
    data = json.loads(_FIXTURE.read_text())
    stride = data["stride"]
    sprites = data["sprites"]
    assert sprites, "empty golden fixture"
    for it in sprites:
        spr = Sprite(**it["spr"])
        attr = SpriteAttr(**it["attr"])
        cam = Camera(**it["cam"])
        draw = plan_sprite(spr, attr, cam)
        assert draw is not None, f"recovered planner culled a sprite the ASM drew: {it['spr']}"

        planes = [bytearray(0x10000) for _ in range(4)]
        for key, vals in it["before"].items():        # seed the dest planes the blit reads/writes
            off = int(key, 16)
            for p in range(4):
                planes[p][off] = vals[p]
        paint_sprite(planes, draw, bytes.fromhex(it["src"]), stride)

        for key, vals in it["after"].items():
            off = int(key, 16)
            for p in range(4):
                assert planes[p][off] == vals[p], (
                    f"sprite {spr.sprite_id:#06x} off {key} plane{p}: "
                    f"got {planes[p][off]:#04x} want {vals[p]:#04x}"
                )


def test_hud_boss_meter_no_camera_0x135():
    """The fixed-screen HUD / boss-meter sprite (id 0x135, 1030:2784) is positioned with NO
    camera offset and skips the off-screen-X / screen_y<=0 culls, unlike a normal sprite.
    RECOVERED from disassembly; pixel fidelity verify-pending on a boss-fight snapshot."""
    cam = Camera(cam_x=100, cam_y=20, fine_scroll=0, row_factor=0, dest_page=0x2000,
                 row_stride=0x28, global_shift=0, frame=1)
    attr = SpriteAttr(width=8, height=12, x_off=4, y_off=0, src_seg=0x650A, src_off=0x0BC7)
    # world_x=50 with cam_x=100: a NORMAL sprite is far off-left -> culled.
    normal = plan_sprite(Sprite(x=50, y=120, sprite_id=0x100, flags=0, life=10), attr, cam)
    assert normal is None, "control: a normal sprite here is off-camera and culled"
    # the HUD sprite (0x135) is drawn at the fixed screen position world_x - x_off (no camera).
    draw = plan_sprite(Sprite(x=50, y=120, sprite_id=0x135, flags=0, life=10), attr, cam)
    assert draw is not None, "HUD/boss-meter sprite must NOT be culled off-camera"
    assert draw.shift == (50 - 4) & 7, "HUD screen_x must be world_x - x_off (no camera)"
    # the flag bits (drawn/flash) don't change the special-case selection.
    assert plan_sprite(Sprite(x=50, y=120, sprite_id=0x2135, flags=0, life=10), attr, cam) is not None


def test_object_render_clipped_flip_byte_exact():
    """The clipped + H-flipped (1030:2AA1) variant — captured from a flipped player
    pushed against a screen edge. Reads the source row right-to-left from the full
    row end, bit-mirrored, with the right-edge final-carry suppressed."""
    it = json.loads((Path(__file__).parent / "fixtures" / "object_render_clipflip.json").read_text())
    draw = SpriteDraw(
        sprite_id=0, src_seg=it["src_seg"], src_off=it["src_off"], dest_off=it["dest"],
        byte_width=it["bw"], rows=it["rows"], shift=it["shift"], flipped=True,
        mode=it["mode"], clipped=True, src_bw=it["src_bw"],
        left_skip=it["ls"], right_skip=it["rs"], right_clipped=it["rclip"],
        full_rows=it.get("full_rows", it["rows"]),
    )
    planes = [bytearray(0x10000) for _ in range(4)]
    for key, vals in it["before"].items():
        off = int(key, 16)
        for p in range(4):
            planes[p][off] = vals[p]
    paint_sprite(planes, draw, bytes.fromhex(it["src"]), 40)
    for key, vals in it["after"].items():
        off = int(key, 16)
        for p in range(4):
            assert planes[p][off] == vals[p], f"off {key} plane{p}: got {planes[p][off]:#04x} want {vals[p]:#04x}"


def test_object_render_top_clip_byte_exact():
    """Top/bottom-clipped sprite (1030:2811 / 2849) — the source plane-block stride is the
    FULL sprite height (full_rows*src_bw), not the clipped row count. Captured from a sprite
    partially above the screen top."""
    it = json.loads((Path(__file__).parent / "fixtures" / "object_render_topclip.json").read_text())
    draw = SpriteDraw(**it["draw"])
    assert draw.full_rows > draw.rows  # genuinely top/bottom-clipped
    planes = [bytearray(0x10000) for _ in range(4)]
    for key, vals in it["before"].items():
        off = int(key, 16)
        for p in range(4):
            planes[p][off] = vals[p]
    paint_sprite(planes, draw, bytes.fromhex(it["src"]), 40)
    for key, vals in it["after"].items():
        off = int(key, 16)
        for p in range(4):
            assert planes[p][off] == vals[p], f"off {key} plane{p}: got {planes[p][off]:#04x} want {vals[p]:#04x}"
