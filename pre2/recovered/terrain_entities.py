"""The terrain / moving-entity system (1030:4907) — a separate sub-island from the effects-update pass.

Once per frame the main loop (1030:022C) walks the 16-slot source list at DS:0x9107 (SOURCE STRIDE 0xF), and
for each live slot ([+4]!=0xFFFF) dispatches by type ([+6]&0xF):

  * type 8  -> a falling/settling object (a 3-state machine [+0xA]: 0 wait -> 1 fall+accelerate until it lands
               on a solid tile -> 2 settle for [+0xD] frames -> 0), [asm 492D..49F2]
  * other   -> an 8-direction moving platform (speed [+0xE] ramps toward target [+7]; direction [+6]&7 picks
               one of the 8 compass headings; oscillates by an anim counter [+0xC] vs [+0xA]), [asm 49F3..4A72]

then projects on-screen slots into the render array DS:0x5570 (stride 0x12, max 7) and runs the player-ride
collision (4B05) — the player can stand on / be pushed by these. THIS MODULE currently recovers the per-slot
MOVEMENT half (the source-slot writes); the render projection + player-ride collision land next.

Pure: ``rw``/``rb`` read DS words/bytes, ``read_tile(off)`` reads the level-map (es=[0x2DDA]) segment. Each
transform returns a ``{offset: (value, width)}`` contract.
"""
from __future__ import annotations

from pre2.islands import oracle_link

ENTITY_LO = 0x9107
ENTITY_N = 0x10
SRC_STRIDE = 0xF             # [asm 4AF0] add si,0xF
MAP_HEIGHT = 0x2CF5         # [0x2CF5] map height (tiles)
MAP_SEG_PTR = 0x2DDA
TBL_FLOOR = 0x7F5E


def _s16(v):
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _sar16(v, n):
    return (_s16(v) >> n) & 0xFFFF


# (vx_sign, vy_sign) for direction [+6]&7 — start speed in both, then the 4A2A..4A5F negate/zero pattern.
_DIR8 = {0: (0, -1), 1: (1, -1), 2: (1, 0), 3: (1, 1), 4: (0, 1), 5: (-1, 1), 6: (-1, 0), 7: (-1, -1)}


def _move_type8(rw, rb, read_tile, b):
    """[asm 492D..49F2] one falling/settling (type-8) entity. Returns its source-slot writes (excl [+6])."""
    w: dict[int, tuple[int, int]] = {}
    vel0 = rw((b + 0xB) & 0xFFFF)
    y_mem = (rw((b + 2) & 0xFFFF) - vel0) & 0xFFFF            # [asm 4936] Y -= vel (pre)
    state = rb((b + 0xA) & 0xFFFF)
    s7 = rb((b + 7) & 0xFFFF)
    timer = rb((b + 0xD) & 0xFFFF)
    vel = vel0

    if state == 0:                                           # [asm 4939] wait / brake
        nv = _s16(vel0) - 8                                  # [asm 4942]
        if nv < 0:
            nv = 0
            s7 = 0
            w[(b + 7) & 0xFFFF] = (0, 1)                     # [asm 4949]
        vel = nv & 0xFFFF
        w[(b + 0xB) & 0xFFFF] = (vel, 2)
        if rb((b + 6) & 0xFFFF) & 0x40:                      # [asm 4950] resting on ground
            settle = True
            if s7 == 0:                                      # [asm 4956]
                timer = (timer - 1) & 0xFF
                w[(b + 0xD) & 0xFFFF] = (timer, 1)
                if timer != 0:
                    settle = False
            if settle:
                w[(b + 0xA) & 0xFFFF] = (1, 1)               # [asm 4961] -> fall
                w[(b + 7) & 0xFFFF] = (0, 1)
    elif state == 1:                                         # [asm 496B] fall + accelerate, land on solid
        if s7 < 0xC0:                                        # [asm 4976]
            s7 = (s7 + 8) & 0xFF
            w[(b + 7) & 0xFFFF] = (s7, 1)
        accel = s7 >> 4                                      # [asm 4983]
        vel = (vel0 + accel) & 0xFFFF                        # [asm 4988]
        w[(b + 0xB) & 0xFFFF] = (vel, 2)
        col = _sar16(rw(b & 0xFFFF), 4) & 0xFF               # [asm 498F] X tile
        nrow = _sar16((y_mem + vel) & 0xFFFF, 4)             # [asm 4993] next Y tile
        bound = (rb(MAP_HEIGHT) - 1) & 0xFFFF                # [asm 499B-49A0]
        settle = False
        if bound >= nrow:                                    # [asm 49A3] jae (in vertical bounds)
            tile = read_tile((col | ((nrow & 0xFF) << 8)) & 0xFFFF)   # [asm 49B0]
            prop = rb((TBL_FLOOR + tile) & 0xFFFF)
            if prop != 0 and prop != 6:                      # [asm 49B5-49C1] solid (not pass/special)
                settle = True
        elif ((bound - nrow) & 0xFFFF) >= 0x10000 - 3 or (nrow - bound) >= 3:  # [asm 49A5-49AC]
            settle = True
        if settle:
            w[(b + 0xA) & 0xFFFF] = (2, 1)                   # [asm 49C3] -> settle
            w[(b + 0xD) & 0xFFFF] = (0x16, 1)
    elif state == 2:                                         # [asm 49CF] settle countdown
        if not (rb((b + 6) & 0xFFFF) & 0x40):                # [asm 49D5]
            timer = (timer - 1) & 0xFF
            w[(b + 0xD) & 0xFFFF] = (timer, 1)
            if timer == 0:
                w[(b + 0xA) & 0xFFFF] = (0, 1)               # [asm 49E0] -> wait
                w[(b + 0xD) & 0xFFFF] = (rb((b + 9) & 0xFFFF), 1)

    w[(b + 2) & 0xFFFF] = ((y_mem + vel) & 0xFFFF, 2)        # [asm 49EA] Y += vel (apply)
    return w


def _move_default(rw, rb, b):
    """[asm 49F3..4A72] one 8-direction moving platform. Returns its source-slot writes (excl [+6])."""
    w: dict[int, tuple[int, int]] = {}
    spd = rb((b + 0xE) & 0xFFFF)
    s7 = rb((b + 7) & 0xFFFF)
    if spd == 0 and _s16(s7 | (0 if s7 < 0x80 else 0xFF00)) >= 0 and not (rb((b + 6) & 0xFFFF) & 0xC0):
        return w                                             # [asm 49FB-4A0B] stationary -> no movement

    al = spd                                                 # [asm 4A0D] ramp speed toward target [+7]
    if s7 != al:
        al = (al + (1 if _s16_b(s7) >= _s16_b(al) else -1)) & 0xFF
        w[(b + 0xE) & 0xFFFF] = (al, 1)
    sp = al                                                  # [asm 4A20] magnitude
    sx, sy = _DIR8[rb((b + 6) & 0xFFFF) & 7]                 # [asm 4A22-4A5F] direction
    vx = _s8(sp) * sx
    vy = _s8(sp) * sy
    x = (rw(b & 0xFFFF) + (vx & 0xFFFF)) & 0xFFFF            # [asm 4A61-4A67] X += vx (sign-extended)
    w[b & 0xFFFF] = (x, 2)
    y = (rw((b + 2) & 0xFFFF) + (vy & 0xFFFF)) & 0xFFFF      # [asm 4A69-4A6F] Y += vy
    w[(b + 2) & 0xFFFF] = (y, 2)

    if s7 == al:                                             # [asm 4A72] at target speed -> dwell/oscillate
        cnt = (rw((b + 0xC) & 0xFFFF) + 1) & 0xFFFF
        if rw((b + 0xA) & 0xFFFF) != cnt:                    # [asm 4A7E]
            w[(b + 0xC) & 0xFFFF] = (cnt, 2)
        else:
            w[(b + 7) & 0xFFFF] = ((-_s16_b(s7)) & 0xFF, 1)  # [asm 4A83] reverse direction
            w[(b + 0xC) & 0xFFFF] = (0, 2)
    return w


def _s8(v):
    v &= 0xFF
    return v - 0x100 if v & 0x80 else v


def _s16_b(v):
    return _s8(v)


@oracle_link("1030:4907",
             "per-frame movement of the 16-slot terrain-entity list 0x9107 (source stride 0xF): per live slot "
             "dispatch by [+6]&0xF — type 8 = falling/settling 3-state object ([+0xA]); else 8-direction "
             "moving platform (speed [+0xE] ramps to target [+7], heading [+6]&7, oscillates [+0xC] vs [+0xA]). "
             "This is the MOVEMENT half (source-slot writes); render projection (0x5570) + player-ride "
             "collision (4B05) are recovered separately.",
             "OBSERVED", merge_target="terrain_entities")
def move_entities(rw, rb, read_tile):
    """[asm 4907 movement half] Returns the merged source-slot write contract for all live slots."""
    writes: dict[int, tuple[int, int]] = {}
    b = ENTITY_LO
    for _ in range(ENTITY_N):
        if rw((b + 4) & 0xFFFF) != 0xFFFF:
            if (rb((b + 6) & 0xFFFF) & 0xF) == 8:
                writes.update(_move_type8(rw, rb, read_tile, b))
            else:
                writes.update(_move_default(rw, rb, b))
        b = (b + SRC_STRIDE) & 0xFFFF
    return writes
