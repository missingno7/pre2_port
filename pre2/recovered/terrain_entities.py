"""The terrain / moving-entity system (1030:4907) — a separate sub-island from the effects-update pass.

Once per frame the main loop (1030:022C) walks the 16-slot source list at DS:0x9107 (SOURCE STRIDE 0xF). For
each live slot ([+4]!=0xFFFF) it (1) MOVES the entity, dispatching on [+6]&0xF:

  * type 8  -> a falling/settling object (3-state machine [+0xA]: 0 wait/brake -> 1 fall+accelerate until it
               lands on a solid tile -> 2 settle for [+0xD] frames -> 0), [asm 492D..49F2]
  * other   -> an 8-direction moving platform (speed [+0xE] ramps to target [+7]; heading [+6]&7; oscillates
               [+0xC] vs [+0xA]; stationary early-out), [asm 49F3..4A72]

then (2) PROJECTS on-screen slots into the render array DS:0x5570 (stride 0x12, compacted, budget 7, terminator
[+4]=0xFFFF, back-ref [di+9]=si) vs camera [0x2DE4]/[0x2DE6], and (3) runs the player-RIDE collision (4B05):
hitbox overlap (recovered 8D7B); on contact snap the player onto the entity top and inherit its velocity.

Recovered as one whole-routine transform over a read-through overlay (each stage sees the prior stage's
writes), exactly like the player-interaction pass. ``rw``/``rb`` read DS words/bytes; ``read_tile`` reads the
level-map (es=[0x2DDA]) segment. Returns a byte-level ``{offset: value}`` contract.
"""
from __future__ import annotations

from pre2.views.dgroup_view import OverlayBackend, PlayerGlobals, PlayerView, RenderSlot, TerrainEntity
from pre2.views.tables import Tables
from pre2.islands import oracle_link
from pre2.recovered.combat_interaction import hitbox_overlap

ENTITY_LO = 0x9107
ENTITY_N = 0x10
SRC_STRIDE = 0xF             # [asm 4AF0] add si,0xF
RENDER_LO = 0x5570
RENDER_STRIDE = 0x12
RENDER_BUDGET = 7           # [asm 490D] bx


def _s16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _sar16(v, n):
    return (_s16(v) >> n) & 0xFFFF


def _s8(v):
    v &= 0xFF
    return v - 0x100 if v & 0x80 else v


# (vx_sign, vy_sign) for direction [+6]&7 — the 4A2A..4A5F negate/zero pattern over (speed, speed).
_DIR8 = {0: (0, -1), 1: (1, -1), 2: (1, 0), 3: (1, 1), 4: (0, 1), 5: (-1, 1), 6: (-1, 0), 7: (-1, -1)}


class _Ov(OverlayBackend):
    """The terrain pass's read-through overlay — the shared ``OverlayBackend`` (rb/rw/wb/ww + the ``writes``
    contract) plus the level-map ``tile`` reader this island also needs."""
    __slots__ = ("tile",)

    def __init__(self, rb, read_tile):
        super().__init__(rb)
        self.tile = read_tile


def _move_type8(ov, b):
    """[asm 492D..49F2] falling/settling (type-8) entity movement -> source-slot + scratch writes."""
    g = PlayerGlobals(ov)
    ent = TerrainEntity(ov, b)
    g.entity_vx_scratch = 0                                   # [asm 492D]
    vel0 = ent.fall_vel
    y_mem = (ent.y - vel0) & 0xFFFF                            # [asm 4936] Y -= vel
    ent.y = y_mem
    state = ent.state
    s7 = ent.speed_ramp
    vel = vel0

    if state == 0:                                           # [asm 4939]
        nv = _s16(vel0) - 8                                  # [asm 4942]
        if nv < 0:
            nv = 0
            s7 = 0
            ent.speed_ramp = 0                                # [asm 4949]
        vel = nv & 0xFFFF
        ent.fall_vel = vel
        if ent.type_dir & 0x40:                               # [asm 4950]
            settle = True
            if s7 == 0:                                      # [asm 4956]
                t = (ent.settle_timer - 1) & 0xFF
                ent.settle_timer = t
                if t != 0:
                    settle = False
            if settle:
                ent.state = 1                                 # [asm 4961]
                ent.speed_ramp = 0
    elif state == 1:                                         # [asm 496B]
        if s7 < 0xC0:                                        # [asm 4976]
            s7 = (s7 + 8) & 0xFF
            ent.speed_ramp = s7
        accel = s7 >> 4                                      # [asm 4983]
        g.entity_vy_scratch = accel                          # [asm 4985]
        vel = (vel0 + accel) & 0xFFFF                        # [asm 4988]
        ent.fall_vel = vel
        col = _sar16(ent.x, 4) & 0xFF                         # [asm 498F]
        nrow = _sar16((y_mem + vel) & 0xFFFF, 4)             # [asm 4993]
        bound = (g.map_rows - 1) & 0xFFFF                    # [asm 499B-49A0]
        settle = False
        if bound >= nrow:                                    # [asm 49A3] jae (in bounds)
            tile = ov.tile((col | ((nrow & 0xFF) << 8)) & 0xFFFF)   # [asm 49B0]
            prop = Tables(ov.rb).floor_props[tile]
            if prop != 0 and prop != 6:                      # [asm 49B5-49C1] solid
                settle = True
        elif (nrow - bound) >= 3:                            # [asm 49A5-49AC]
            settle = True
        if settle:
            ent.state = 2                                     # [asm 49C3]
            ent.settle_timer = 0x16
    elif state == 2:                                         # [asm 49CF]
        if not (ent.type_dir & 0x40):                         # [asm 49D5]
            t = (ent.settle_timer - 1) & 0xFF
            ent.settle_timer = t
            if t == 0:
                ent.state = 0                                 # [asm 49E0]
                ent.settle_timer = ent.settle_reload

    ent.y = (y_mem + vel) & 0xFFFF                            # [asm 49EA] Y += vel


def _move_default(ov, b):
    """[asm 49F3..4A72] 8-direction moving-platform movement -> source-slot + scratch writes.

    The dwell/oscillate tail (``s7 == al``) reads/writes ``[+7]``/``[+0xA]``/``[+0xC]`` as full WORDS —
    a THIRD interpretation of those bytes distinct from both the type-8 family's byte fields
    (``speed_ramp``/``state``/``osc``, read earlier in THIS same function as bytes) and from
    :class:`TerrainEntity`'s declared widths. Genuinely a per-branch union of the same storage; kept as raw
    offsets rather than forced through the byte-typed struct fields, which would silently narrow the width."""
    g = PlayerGlobals(ov)
    ent = TerrainEntity(ov, b)
    g.entity_vx_scratch = 0                                   # [asm 49F5/49F8]
    g.entity_vy_scratch = 0
    spd = ent.speed
    s7 = ent.speed_ramp
    if spd == 0 and _s8(s7) >= 0 and not (ent.type_dir & 0xC0):
        return                                               # [asm 49FB-4A0B] stationary

    al = spd                                                 # [asm 4A0D] ramp speed toward [+7]
    if s7 != al:
        al = (al + (1 if _s8(s7) >= _s8(al) else -1)) & 0xFF
        ent.speed = al
    sx, sy = _DIR8[ent.type_dir & 7]                          # [asm 4A22-4A5F]
    vx = (_s8(al) * sx) & 0xFFFF
    vy = (_s8(al) * sy) & 0xFFFF
    g.entity_vx_scratch = vx                                  # [asm 4A64]
    ent.x = (ent.x + vx) & 0xFFFF                             # [asm 4A67] X += vx
    g.entity_vy_scratch = vy                                  # [asm 4A6C]
    ent.y = (ent.y + vy) & 0xFFFF                             # [asm 4A6F] Y += vy

    if s7 == al:                                             # [asm 4A72] at target -> dwell/oscillate
        cnt = (ov.rw((b + 0xC) & 0xFFFF) + 1) & 0xFFFF
        if ov.rw((b + 0xA) & 0xFFFF) != cnt:                 # [asm 4A7E]
            ov.ww((b + 0xC) & 0xFFFF, cnt)
        else:
            ov.ww((b + 7) & 0xFFFF, (-ov.rw((b + 7) & 0xFFFF)) & 0xFFFF)   # [asm 4A83] neg WORD [si+7] (reverse dir)
            ov.ww((b + 0xC) & 0xFFFF, 0)


def _collision_4b05(ov, di):
    """[asm 4B05] player-ride collision. Writes player state on contact; returns CF (True = the player rode)."""
    p, g = PlayerView(ov), PlayerGlobals(ov)
    if g.unk_6BFE != 0:                                       # [asm 4B08]
        return False
    ride_src = RenderSlot(ov, di)
    if _s16(ride_src.y) <= _s16(p.y):                        # [asm 4B0F-4B15]
        return False
    saved_y = p.y
    yadj = g.entity_vy_scratch
    temp_y = (saved_y + yadj) & 0xFFFF if _s16(yadj) >= 0 else saved_y   # [asm 4B1E-4B25]

    def trb(o):                                              # [asm 4B29] player Y=temp_y, sprite [+4]=7
        o &= 0xFFFF
        if o == (p.offset + 4) & 0xFFFF:
            return 7
        if o == (p.offset + 5) & 0xFFFF:
            return 0
        if o == 0x4F1E:
            return temp_y & 0xFF
        if o == 0x4F1F:
            return (temp_y >> 8) & 0xFF
        return ov.rb(o)
    trw = lambda o: trb(o) | (trb((o + 1) & 0xFFFF) << 8)    # noqa: E731
    hit, hb = hitbox_overlap(trb, trw, p.offset, di)         # [asm 4B31] 8D7B
    ov.apply(hb)
    if not hit:                                              # [asm 4B3B]
        return False

    g.unk_6BFE = 1                                            # [asm 4B3D]
    p.motion_mode = 0                                        # [asm 4B42]
    p.x = (p.x + g.entity_vx_scratch) & 0xFFFF               # [asm 4B47] push player X
    # 64DF landing reset
    g.fall_grace = max(g.fall_grace - 1, 0)                  # [asm 64DF] saturating dec
    g.fall_latch = 0
    g.airborne = 2
    g.last_land_y = p.y
    # snap onto the entity top or inherit its velocity
    ride = RenderSlot(ov, di)                                # the entity's render record
    top = (ride.y - Tables(ov.rb).sprite_half_h(ride.sprite)) & 0xFFFF   # [asm 4B51-4B61]
    if _s16(p.y) <= _s16(top):                               # [asm 4B64] jle
        p.yvel = (g.entity_vy_scratch << 4) & 0xFFFF          # [asm 4B76]
    else:
        p.y = (top + 1) & 0xFFFF                             # [asm 4B6A]
        p.yvel = 1
    return True


@oracle_link("1030:4907",
             "per-frame tick of the 16-slot terrain-entity list 0x9107 (source stride 0xF): per live slot move "
             "(type 8 = falling/settling 3-state object; else 8-direction moving platform), project on-screen "
             "slots into the render array 0x5570 (stride 0x12, budget 7, vs camera [0x2DE4]/[0x2DE6]), and run "
             "the player-ride collision 4B05 (hitbox 8D7B; snap player onto the entity top, inherit velocity). "
             "Whole-routine transform over a read-through overlay; returns a byte-level write contract.",
             "OBSERVED", merge_target="terrain_entities")
def tick_terrain_entities(rw, rb, read_tile):
    """[asm 4907] ``rw``/``rb`` read DS word/byte, ``read_tile`` the level-map. Returns ``{offset: value}``."""
    ov = _Ov(rb, read_tile)
    g, p = PlayerGlobals(ov), PlayerView(ov)
    g.unk_6BFE = 0                                            # [asm 4913]
    si = ENTITY_LO
    di = RENDER_LO
    budget = RENDER_BUDGET
    for _ in range(ENTITY_N):
        ent = TerrainEntity(ov, si)
        if ent.sprite == 0xFFFF:                              # [asm 4918] inactive -> di NOT advanced
            si = (si + SRC_STRIDE) & 0xFFFF
            continue
        if (ent.type_dir & 0xF) == 8:                         # [asm 4921] dispatch
            _move_type8(ov, si)
        else:
            _move_default(ov, si)

        dst = RenderSlot(ov, di)                             # the render slot this entity projects into
        sx = (ent.x - ((g.cam_col_word << 4) & 0xFFFF)) & 0xFFFF      # [asm 4A8B] screen X
        if not (_s16(sx) >= 0x160 or _s16(sx) <= -0x20):     # [asm 4A9D-4AA6] on-screen X
            dst.x = ent.x                                     # [asm 4AA8] worldX (even if Y off)
            sy = (ent.y - ((g.cam_row_word << 4) & 0xFFFF)) & 0xFFFF   # [asm 4AAA]
            if not (_s16(sy) >= 0xD0 or _s16(sy) <= -0x20):  # [asm 4ABD-4AC6] on-screen Y
                dst.y = ent.y                                 # [asm 4AC8]
                dst.sprite = ent.sprite                       # [asm 4ACB]
                dst.source = si                              # [asm 4AD1] back-ref to the source slot
                collided = False
                if _s16(p.yvel) > -0x10:                     # [asm 4AD4] jle skips the collision
                    ent.type_dir = ent.type_dir | 0x40        # [asm 4ADB]
                    collided = _collision_4b05(ov, di)       # [asm 4ADF]
                if not collided:                             # [asm 4AE2 jb skips this]
                    ent.type_dir = ent.type_dir & 0xBF        # [asm 4AE4]
                    dst.y = (dst.y - 2) & 0xFFFF             # [asm 4AE8] nudge up 2px
                di = (di + RENDER_STRIDE) & 0xFFFF           # [asm 4AEC]
                budget = (budget - 1) & 0xFFFF
        si = (si + SRC_STRIDE) & 0xFFFF
    while budget != 0:                                       # [asm 4AF9] terminate the remaining render slots
        RenderSlot(ov, di).sprite = 0xFFFF                   # dead marker
        di = (di + RENDER_STRIDE) & 0xFFFF
        budget = (budget - 1) & 0xFFFF
    return ov.writes
