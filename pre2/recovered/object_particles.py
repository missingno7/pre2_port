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

from pre2.views.dgroup_view import EffectSource, RenderSlot, WidthContractBackend
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
# VERTICAL item zone (SMOOTH CAMERA): the Y window's TOP edge is tight (``ayb < cam_y`` drops anything above the
# camera; the bottom WIN_Y is already loose). The smooth camera's vertical drag (v_pad) brings above/below-camera
# items into the widescreen CORNERS, where the faithful projector never bounced them (not projected -> frozen Y).
# Widen the Y cull by these tiles so those items PROJECT + BOUNCE live, exactly like the X margins. 0 = faithful.
_ITEM_ZONE_Y = [0, 0]     # [top, bottom] extra tiles


def set_item_zone_margins(left: int, right: int) -> None:
    """Widen :func:`project_particles`' X window by ``left``/``right`` tiles (0,0 = faithful). Asymmetric so it
    tracks the true-widescreen margin slide near a world edge -> only the VISIBLE window projects, not a wasteful
    symmetric 2*margin that would burn the 20-slot budget. Runtime-set per frame."""
    _ITEM_ZONE_X[0] = max(0, int(left))
    _ITEM_ZONE_X[1] = max(0, int(right))


def set_item_zone_y_margins(top: int, bottom: int) -> None:
    """Widen :func:`project_particles`' Y window by ``top``/``bottom`` tiles (0,0 = faithful). Sized from the
    smooth camera's vertical deviation band (v_pad) so a pickup the camera drags into a top/bottom CORNER keeps
    bouncing (projected live) instead of freezing as a re-projected margin record. Runtime-set per frame."""
    _ITEM_ZONE_Y[0] = max(0, int(top))
    _ITEM_ZONE_Y[1] = max(0, int(bottom))


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

    Two X-culled PASSES, not one: items within the FAITHFUL window (``0 <= sxt <= WIN_X`` — what the ORIGINAL
    game already shows, at most 20 of them by the original's own design) always get a slot first; only once
    those are all placed does a second pass spend any REMAINING slots on widescreen-MARGIN-only items (``sxt``
    outside [0, WIN_X] but inside the zone widened by :func:`set_item_zone_margins`). At the faithful zone
    ([0, 0]) the margin-only set is empty by construction, so this is a no-op — byte-identical single pass, in
    list order, exactly as the ASM's own one-pass loop. But once a dense item cluster sits in the WIDESCREEN
    margin (its own X range never faithfully visible together with a far-off item), a single list-order pass
    let it exhaust the 20-slot budget before reaching a later, faithfully-visible item further down the list
    — e.g. LEVEL1's exit semaphore (list index 69, the very last entry) losing its slot to margin-only clutter
    near the player. Prioritizing the faithful set first means an item the ORIGINAL game would show is never
    starved by one only the widescreen enhancement added."""
    cam_x = rw(CAM_X)
    cam_y = rw(CAM_Y)
    no_anim = (rb(FREEZE_FLAG) & 1) != 0

    be = WidthContractBackend(rb, rw)   # accumulates the {offset: (value, width)} contract
    state = {"di": DST_SLOTS, "bx": DST_COUNT, "filled_all": False}

    def _project(si) -> None:
        src = EffectSource(be, si)                   # the source entry, by name (reads raw, writes the contract)
        slot = RenderSlot(be, state["di"])
        slot.x = src.x                               # [asm 8955] on-screen: copy raw X into the render slot

        # [asm 8959..8974] bounce animation -> ax (added to Y)
        ax = 0
        if not no_anim:
            al = src.bounce
            ax = (_s8(al) + 1) & 0xFFFF  # cbw ; inc ax
            src.bounce = ax
            if _s8(ax & 0xFF) >= 4:  # cmp al,4 ; jl skips
                ax = (-ax + 1) & 0xFFFF  # neg ax ; inc ax
                src.bounce = ax

        # [asm 8974] new Y written back to the source AND into the slot
        new_y = (ax + src.y) & 0xFFFF
        src.y = new_y
        slot.y = new_y
        slot.sprite = src.sprite                     # [asm 897D] sprite id ...
        slot.source = si                             # ... + back-reference to the source entry

        state["di"] += DST_STRIDE
        state["bx"] -= 1
        if state["bx"] == 0:  # [asm 8989] all slots filled -> done, no clearing
            state["filled_all"] = True

    def _pass(faithful: bool) -> bool:
        """One sweep over the source list; ``faithful`` selects [0,WIN_X] vs margin-only. Returns True if the
        slot budget ran out (caller should stop)."""
        for k in range(SRC_COUNT):
            if state["filled_all"]:
                return True
            si = SRC_LIST + k * SRC_STRIDE
            src = EffectSource(be, si)

            if src.sprite == 0xFFFF:  # [asm 8930] empty source entry
                continue

            # [asm 8936] screen-X cull: ax = (X >> 4) - cam_x ; jb / jg out of window (widened each side by
            # the widescreen item zone; m=0 is byte-exact with the ASM: sxt<0 == the jb 'axb<cam_x', sxt>WIN_X
            # == the jg)
            axb = (_s16(src.x) >> 4) & 0xFFFF
            sxt = _s16((axb - cam_x) & 0xFFFF)
            if faithful:
                if sxt < 0 or sxt > WIN_X:
                    continue
            else:
                if 0 <= sxt <= WIN_X or sxt < -_ITEM_ZONE_X[0] or sxt > WIN_X + _ITEM_ZONE_X[1]:
                    continue

            # [asm 8945] screen-Y cull (top/bottom widened by the smooth-camera vertical item zone; [0,0] is
            # byte-exact: sy<0 == the 'jb ayb<cam_y', sy>WIN_Y == the jg)
            ayb = (_s16(src.y) >> 4) & 0xFFFF
            sy = _s16((ayb - cam_y) & 0xFFFF)
            if sy < -_ITEM_ZONE_Y[0] or sy > WIN_Y + _ITEM_ZONE_Y[1]:
                continue

            _project(si)
        return state["filled_all"]

    if not _pass(faithful=True):
        _pass(faithful=False)

    # [asm 8992] clear the unused tail slots
    if not state["filled_all"]:
        di, bx = state["di"], state["bx"]
        while bx > 0:
            RenderSlot(be, di).sprite = 0xFFFF
            di += DST_STRIDE
            bx -= 1

    return be.writes
