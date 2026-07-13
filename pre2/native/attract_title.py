"""The menu-idle ATTRACT title animation — recovered native driver + render for [asm 8F2D..9046].

After the mode-select menu sits idle past the timeout ([0x27F0] >= 0x10E), the VM plays a scripted
2-phase animation over the jungle "PREHISTORIK 2" title (MENU2.SQZ, a 16-colour planar EGA image):
  * PHASE 1 (8F94..8FC4): a caveman runs RIGHT across the screen chased by a dino, until the caveman
    scrolls off the right edge.
  * PHASE 2 (8FC6..9046): the dino record is respawned as 3 staggered bouncing objects and the caveman
    runs BACK LEFT until off the left edge.

The animation is byte/pixel-exact vs the VM: it composes the recovered anim steppers (638B caveman,
9047 generic object) with the recovered sprite renderer (plan_frame + paint_sprite) over the decoded
title base. Verified frame-for-frame (119 frames, 0 pixel diffs) by tests/test_attract_title.py.

The caveman lives in the PLAYER slot 0x4F1C; the dino / bounce objects in slots 0x4F2E/0x4F40/0x4F52
(the active-sprite records the renderer already walks). [0x2879] stays 0 throughout — this is NOT the
demo-playback path, it is a self-contained scripted scene."""
from __future__ import annotations

from pre2.native.vga import _dac8
from pre2.views.image_scene import title_planar_image
from pre2.views.render_state import read_renderer_state
from pre2.native.state import DATA_SEG
from pre2.recovered.object_render import (Sprite, paint_sprite, plan_frame,
                                          plan_sprite_command)
from pre2.recovered.player import player_advance_anim
from pre2.views.dgroup_view import PlayerGlobals, PlayerView

_DS = DATA_SEG << 4
CAV = 0x4F1C            # caveman = the player slot (active-sprite record)
DINO = 0x4F2E          # dino / first bounce object
_PAGE = 0x2000         # render page (VM double-buffers 2000/0000; a single page renders identically)
_TITLE_ASSET = "MENU2.SQZ"


def _rd(d, off):
    return d[_DS + off] | (d[_DS + off + 1] << 8)


def _ww(d, off, val):
    d[_DS + off] = val & 0xFF
    d[_DS + off + 1] = (val >> 8) & 0xFF


def _s16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _caveman_anim(state):
    """[asm 8F9B/9002] 638B — advance the caveman's animation frame from its script pointer [0x4F28]. The
    caveman is the player render slot, so its anim fields are the named PlayerView."""
    pv, g = PlayerView(state), PlayerGlobals(state)
    frame, new_ptr, bcf = player_advance_anim(pv.anim_ptr, pv.facing_lo, state.rw)
    pv.sprite = frame; pv.anim_ptr = new_ptr; g.anim_hi = bcf


def _advance_obj_anim(d, si):
    """[asm 9047] generic object animation stepper: ``bx=[si+0xc]; ax=[bx]; if ax<0: bx+=ax,ax=[bx];
    ah&=0x1f; ax+=0x138; ah|=[si+9]&0x80; [si+4]=ax; [si+0xc]=bx+2``. (Distinct from 638B: adds the 0x138
    sprite-id base and does not re-mask after the add.)"""
    bx = _rd(d, si + 0xC)
    ax = _rd(d, bx)
    if ax & 0x8000:                                  # [9051] negative = loop marker: rewind + reload
        bx = (bx + _s16(ax)) & 0xFFFF
        ax = _rd(d, bx)
    ax = ((ax & 0x1FFF) + 0x138) & 0xFFFF            # [9057] ah&=0x1f (keep low byte); [905A] +0x138
    ax = (ax | ((d[_DS + si + 9] & 0x80) << 8)) & 0xFFFF   # [905D/9063] ah |= [si+9]&0x80 (facing)
    _ww(d, si + 4, ax)                               # [9065] [si+4] = ax
    _ww(d, si + 0xC, (bx + 2) & 0xFFFF)              # [906A] [si+0xc] = bx+2


def _caveman_onscreen(d, rs):
    """[asm 8FBF test [0x4F21],0x20] Whether 26FA would draw the caveman — the phase-1 loop's exit gate.
    Recomputed from the same placement logic the renderer uses (plan_sprite_command)."""
    sid = _rd(d, CAV + 4)
    attr = (rs.object_attrs or {}).get(sid & 0x1FFF)
    if attr is None:
        return False
    spr = Sprite(x=_rd(d, CAV), y=_rd(d, CAV + 2), sprite_id=sid,
                 flags=d[_DS + CAV + 5], life=d[_DS + CAV + 0x11])
    return plan_sprite_command(spr, attr, rs.object_camera) is not None


def _compose(state, base_planes, pal256):
    """Render one frame: the jungle base + the caveman/dino sprites (plan_frame + paint_sprite), as a
    FrontEndScene-ready (planes, page, palette). Uses the recovered renderer over the decoded base."""
    d = state.data
    _ww(d, 0x2DD8, _PAGE)                             # pin the render page so cam.dest_page is stable
    planes = [bytearray(0x10000) for _ in range(4)]
    for p in range(4):
        planes[p][_PAGE:_PAGE + len(base_planes[p])] = base_planes[p]
    rs = read_renderer_state(state, frame_pre_inc=False)
    cam = rs.object_camera
    banks = rs.object_src_banks or {}
    for draw in plan_frame(rs.object_sprites, rs.object_attrs or {}, cam):
        bank = banks.get(draw.src_seg, b"")
        size = draw.src_bw * draw.full_rows * 6 + 64
        paint_sprite(planes, draw, bank[draw.src_off:draw.src_off + size], cam.row_stride)
    return tuple(bytes(p) for p in planes), rs


def _expand_pal(pal6: bytes) -> tuple:
    """MENU2's 16-entry 6-bit palette -> 256 x (r,g,b) 8-bit DAC (unused entries black)."""
    pal = [(0, 0, 0)] * 256
    for i in range(len(pal6) // 3):
        pal[i] = (_dac8(pal6[i * 3]), _dac8(pal6[i * 3 + 1]), _dac8(pal6[i * 3 + 2]))
    return tuple(pal)


def native_attract_title(state, game_root: str):
    """Drive the attract-title animation, yielding one presentable frame ((mode, palette, planes, page))
    per VM present. Mutates ``state`` (the caveman/dino records) exactly as [asm 8F2D..9046]."""
    from pre2.native.front_end import FrontEndScene
    from pre2.recovered.scene import MODE_PLANAR

    d = state.data
    g, pv = PlayerGlobals(state), PlayerView(state)
    base_planes, pal6 = title_planar_image(_TITLE_ASSET, game_root)
    pal256 = _expand_pal(pal6)

    def frame(planes):
        # game_paced: the VM presents each animation frame via 44FB -> the 3-retrace 1C6F wait (~23.33Hz),
        # NOT the 70Hz single-retrace of the static front-end screens. The runner must pace it likewise.
        return FrontEndScene(MODE_PLANAR, palette=pal256, planes=planes, page=_PAGE, game_paced=True)

    # -------- SETUP (8F2D..8F8F) -------- #
    d[_DS + 0x6C01] = 0; d[_DS + 0x6C02] = 1; d[_DS + 0x6C03] = 0   # [8F46..8F50] byte flags (unnamed)
    g.fine_scroll = 0                                 # [8F55] fine-scroll byte
    g.cam_col_word = 0; g.cam_row_word = 0            # [8F5C/8F5F] camera = origin
    pv.x = 0; pv.y = 0xA9; pv.facing = 0              # caveman (= player slot) X=0 Y=0xA9 facing=right
    pv.xvel = 0; pv.anim_ptr = 0x7BA7                 # Xvel=0, anim-ptr=0x7BA7
    _ww(d, DINO, 0x30); _ww(d, 0x4F30, 0xA9)          # dino X=0x30 Y=0xA9 (the dino/bounce-object arena)
    _ww(d, 0x4F37, 0); _ww(d, 0x4F34, 0); _ww(d, 0x4F3A, 0xAB4B)   # dino flags/anim-ptr
    g.glider = 0

    # -------- PHASE 1 (8F94..8FC4): caveman runs right, dino chases -------- #
    bp = 0
    while True:
        bp = (bp + 1) & 0xFFFF                        # [8F96] inc bp
        _caveman_anim(state)                          # [8F9B] 638B
        _advance_obj_anim(d, DINO)                    # [8FA1] 9047
        planes, rs = _compose(state, base_planes, pal256)   # [8FA4 restore + 8FA7 26FA]
        yield frame(planes)                           # [8FAA] present
        onscreen = _caveman_onscreen(d, rs)           # the [0x4F21]&0x20 the render would set
        pv.x = (pv.x + 5) & 0xFFFF                     # [8FAD] caveman X += 5
        _ww(d, DINO, (_rd(d, DINO) + (5 if (bp & 3) else 4)) & 0xFFFF)  # [8FB4] dino += 5 / 4 every 4th
        if not onscreen:                              # [8FBF/8FC4] exit when caveman off-screen
            break

    # -------- PHASE 2 setup (8FC6..9000): kill dino, spawn 3 staggered bouncing objects -------- #
    _ww(d, 0x4F37, 0xFFFF)                            # [8FC8] (the dino/bounce-object arena)
    _ww(d, 0x4F3C, 0)                                 # [8FCE]
    pv.facing = 0xFFFF                                # [8FD4] caveman facing = left (H-flip)
    for w in range(0x3F):                            # [8FDA] rep movsw 4F2E->4F40, OVERLAPPING -> propagates
        s = 0x4F2E + w * 2; t = 0x4F40 + w * 2       # (replicates the 18-byte dino record across the slots)
        d[_DS + t] = d[_DS + s]; d[_DS + t + 1] = d[_DS + s + 1]
    ax = pv.x; dx = pv.y                             # [8FEB/8FEE]
    for i in range(3):                              # [8FF2..9000] loop target 8FF2 -> step EACH object
        ax = (ax + 0x1E) & 0xFFFF                    # [8FF2] staggered X
        dx = (dx - 3) & 0xFFFF                       # [8FF5] staggered Y
        si = 0x4F2E + i * 0x12
        _ww(d, si, ax); _ww(d, si + 2, dx)

    # -------- PHASE 2 loop (9002..9044): objects fall/bounce, caveman runs back left -------- #
    while _s16(pv.x) >= 0:                            # [9044 jns] while caveman X >= 0
        _caveman_anim(state)                         # [9002] 638B
        for i in range(3):                          # [900F..9034] per bouncing object
            si = 0x4F2E + i * 0x12
            _ww(d, si, (_rd(d, si) - 5) & 0xFFFF)    # [900F] X -= 5
            _advance_obj_anim(d, si)                 # [9012] 9047
            if _rd(d, si + 2) > 0xA9:                # [9015] bounce: Y > 0xA9 -> negate velocity
                _ww(d, si + 0xE, (-_s16(_rd(d, si + 0xE))) & 0xFFFF)
            _ww(d, si + 0xE, (_rd(d, si + 0xE) + 8) & 0xFFFF)         # [901F] vel += 8 (gravity)
            _ww(d, si + 2, (_rd(d, si + 2) + (_s16(_rd(d, si + 0xE)) >> 4)) & 0xFFFF)  # [9023] Y += vel>>4
        planes, _ = _compose(state, base_planes, pal256)   # [9036 restore + 9039 26FA]
        yield frame(planes)                          # [903C] present
        pv.x = (pv.x - 7) & 0xFFFF                    # [903F] caveman X -= 7
