"""Effect/particle projection island — 1030:8922.

[asm 8922]  Once per frame the main loop (call site ~1030:0235) walks a fixed
70-entry effect/particle list at DS:0x8F1D (7 bytes each) and projects every
entry that is currently on-screen into the render-slot array at DS:0x52E8 (up to
20 slots, 0x12 bytes each).  Each projected entry also gets a small vertical
"bounce" animation: a per-entry counter ([+6]) ping-pongs 0,1,2,3 -> -3,-2,-1,0
and is added to the world Y so the effect floats up and down.

Source entry (DS:0x8F1D, stride 7):
    [+0] word  world X
    [+2] word  world Y      (advanced in place by the bounce each frame)
    [+4] word  sprite id    (0xFFFF = empty slot)
    [+6] byte  bounce counter (signed, ping-pong)

Render slot (DS:0x52E8, stride 0x12) — only these fields are written here:
    [+0] word  screen-space X (= source X, raw)
    [+2] word  Y (after bounce)
    [+4] word  sprite id  (0xFFFF = slot unused)
    [+9] word  back-reference to the source entry offset

Pure: reads/writes happen through caller-supplied DS accessors; no VM, no planes.
The animation is gated off when [0x6BD5] & 1 (a global "freeze effects" flag, e.g.
during pause/scripted pose).
"""
from __future__ import annotations

from pre2.bridge.dgroup_view import RenderSlot, WidthContractBackend
from pre2.islands import oracle_link

SRC_LIST = 0x8F1D
SRC_STRIDE = 7
SRC_COUNT = 0x46  # 70

DST_SLOTS = 0x52E8
DST_STRIDE = 0x12
DST_COUNT = 0x14  # 20

CAM_X = 0x2DE4
CAM_Y = 0x2DE6
FREEZE_FLAG = 0x6BD5  # bit 0 -> skip the bounce animation

# screen-window culling bounds (inclusive), in >>4 (tile) units after camera subtract
WIN_X = 0x16
WIN_Y = 0x2B

# WIDESCREEN ITEM ZONE (opt-in): extra tiles the X window widens by so float items (pickups/popups) PROJECT +
# BOUNCE across the widescreen margins, not just the 320 view -- one live system for every visible item (no
# frozen re-projection, no active<->inactive gap at the edge). 0 = the FAITHFUL window (every test + the plain
# runtime). The native runtime sets it per frame from the widescreen margin; items don't affect gameplay, so
# unlike the enemy zone this is safe to enable with true widescreen directly. (The 20-slot render budget still
# caps how many project; the far overflow falls back to the frozen re-projection, unchanged.)
_ITEM_ZONE_X = [0, 0]     # [left, right] extra tiles -- ASYMMETRIC to match the widescreen margin SLIDE exactly


def set_item_zone_margins(left: int, right: int) -> None:
    """Widen :func:`project_particles`' X window by ``left``/``right`` tiles (0,0 = faithful). Asymmetric so it
    tracks the true-widescreen margin slide near a world edge -> only the VISIBLE window projects, not a wasteful
    symmetric 2*margin that would burn the 20-slot budget. Runtime-set per frame."""
    _ITEM_ZONE_X[0] = max(0, int(left))
    _ITEM_ZONE_X[1] = max(0, int(right))


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _s8(v: int) -> int:
    v &= 0xFF
    return v - 0x100 if v & 0x80 else v


@oracle_link("1030:8922",
             "effect-sprite projector: walk the 70-entry list at DS:0x8F1D (X/Y/sprite/bounce, stride 7), "
             "cull each vs camera [0x2DE4]/[0x2DE6] to the on-screen window (<=0x16 x, <=0x2B y), animate the "
             "per-entry bounce counter [+6] (cbw+1 ping-pong 0..3 -> -3..0, gated off by [0x6BD5]&1) into the "
             "world Y [+2], and project the on-screen ones into the render slots at DS:0x52E8 (X/Y/sprite/"
             "back-ref, stride 0x12, max 20); unused tail slots get sprite-id 0xFFFF.",
             "VERIFIED", merge_target="render_frame")
def project_particles(rb, rw):
    """[asm 8922] Animate + project the on-screen effect list into the render slots.

    `rb(off)` / `rw(off)` read a byte / word from DS at the given offset.
    Returns a dict {ds_offset: (value, width)} of every DS byte/word the routine
    writes (source-list bounce updates + the render-slot fields), in the order the
    ASM emits them (the dict is the full side-effect contract).
    """
    cam_x = rw(CAM_X)
    cam_y = rw(CAM_Y)
    no_anim = (rb(FREEZE_FLAG) & 1) != 0

    be = WidthContractBackend(rb, rw)   # accumulates the {offset: (value, width)} contract
    di = DST_SLOTS
    bx = DST_COUNT
    filled_all = False

    for k in range(SRC_COUNT):
        si = SRC_LIST + k * SRC_STRIDE

        if rw(si + 4) == 0xFFFF:  # [asm 8930] empty source entry
            continue

        # [asm 8936] screen-X cull: ax = (X >> 4) - cam_x ; jb / jg out of window (widened each side by the
        # widescreen item zone; m=0 is byte-exact with the ASM: sxt<0 == the jb 'axb<cam_x', sxt>WIN_X == the jg)
        axb = (_s16(rw(si)) >> 4) & 0xFFFF
        sxt = _s16((axb - cam_x) & 0xFFFF)
        if sxt < -_ITEM_ZONE_X[0] or sxt > WIN_X + _ITEM_ZONE_X[1]:
            continue

        # [asm 8945] screen-Y cull
        ayb = (_s16(rw(si + 2)) >> 4) & 0xFFFF
        if ayb < cam_y:
            continue
        sy = (ayb - cam_y) & 0xFFFF
        if _s16(sy) > WIN_Y:
            continue

        slot = RenderSlot(be, di)
        slot.x = rw(si)                              # [asm 8955] on-screen: copy raw X into the render slot

        # [asm 8959..8974] bounce animation -> ax (added to Y)
        ax = 0
        if not no_anim:
            al = rb(si + 6)
            ax = (_s8(al) + 1) & 0xFFFF  # cbw ; inc ax
            be.wb(si + 6, ax)
            if _s8(ax & 0xFF) >= 4:  # cmp al,4 ; jl skips
                ax = (-ax + 1) & 0xFFFF  # neg ax ; inc ax
                be.wb(si + 6, ax)

        # [asm 8974] new Y written back to the source AND into the slot
        new_y = (ax + rw(si + 2)) & 0xFFFF
        be.ww(si + 2, new_y)
        slot.y = new_y
        slot.sprite = rw(si + 4)                     # [asm 897D] sprite id ...
        slot.source = si                             # ... + back-reference to the source entry

        di += DST_STRIDE
        bx -= 1
        if bx == 0:  # [asm 8989] all slots filled -> done, no clearing
            filled_all = True
            break

    # [asm 8992] clear the unused tail slots
    if not filled_all:
        while bx > 0:
            RenderSlot(be, di).sprite = 0xFFFF
            di += DST_STRIDE
            bx -= 1

    return be.writes
