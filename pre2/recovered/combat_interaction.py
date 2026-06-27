"""Combat / pickup interaction island — 1030:88D7 + 899E + 8C21 (in progress).

Once per frame the main loop (~1030:021D) runs the player-and-projectile interaction pass `88D7`: for each of
the 4 projectile slots (DS:0x4F2E, stride 0x12) and then — unless [0x6BC5] (scripted-pose) — the player sprite
(DS:0x4F0A), it runs two collision passes against the source sprite at `si`:

    8C21  source-vs-ENEMY   — scan the 12 active object slots (DS:0x4FD0), sprite-hitbox proximity (8D7B),
                              subtract HP ([di+0xF] -= [0x7B19]); on kill play SFX (0x282) + spawn death debris
                              (8C72 -> 8875), else knockback ([di] -= [di+8]>>2); consume the projectile.
                              Returns CF=1 on a hit (then 88D7 skips 899E for this source).
    899E  source-vs-BONUS   — scan the 80-entry bonus-cell list (DS:0x8C8D, stride 5: [+3]=x cell,[+4]=y cell),
                              proximity (<=1 x cell, <=0x10 y) -> fire the bonus hit handler 8A5A (-> 5E41),
                              choose a score-popup sprite id into [0xA33A], burst score/effect sprites (8D1B),
                              and (for breakable tiles) rewrite + redraw the tile map (8B6E).

This module is being recovered leaf-first (the object_tick precedent). Pure leaves land here with shadow proof;
the 8C21 / 899E parents compose them once every leaf is verified. See docs/pre2/combat_interaction_island.md.

Data tables this island reads (DS): sprite hitbox half-widths [0x7190]/[0x7191]/[0x752A] (indexed by sprite-id
& 0x1F), death-debris count table [0x8C63-ish via bx-0x5C0F], the bonus-cell list 0x8C8D, the enemy object
slots 0x4FD0, and the free effect-object slots 0x50A8..0x52E8.
"""
from __future__ import annotations

from pre2.islands import oracle_link
from pre2.recovered.prng import rng_lcg

# --- globals this island reads/writes -------------------------------------------------
SPAWN_X = 0xA336      # effect-spawn world X (cell << 4)
SPAWN_Y = 0xA338      # effect-spawn world Y (cell << 4)
RNG_STATE = 0x2CEC    # four bytes [0x2CEC..0x2CEF] = rng_lcg state

# sprite-hitbox half-extent tables (DS), indexed by (sprite-id high byte & 0x1F) * 2
HALF_LO = 0x7190      # [+idx] X half-extent (hw2) ; [+idx+1] (0x7191) = Y half-extent
HALF_WX = 0x752A      # [+idx] X half-width
HIT_FLAG = 0xA330     # byte: 1 when a vertical-detail hit was registered (else 0)
HIT_DETAIL = 0xA331   # word: the vertical penetration depth when HIT_FLAG set
PASS_FLAG = 0xA312    # set across the projectile/player pass -> full (un-halved) tolerance
PLAYER_YVEL = 0x4F2A
PLAYER_REC = 0x4F1C   # the player struct base (excluded from the vertical-detail set)


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _abs16(d: int) -> int:
    """The ASM ``jns ; neg`` absolute value of a 16-bit subtraction (0x8000 stays 0x8000)."""
    d &= 0xFFFF
    return (0x10000 - d) if (d & 0x8000) else d


@oracle_link("1030:8BF6",
             "pack-spawn-position: from a bonus/source entry's packed cell coords at [di+3] (x = low byte, "
             "y = high byte), set the effect-spawn world-position globals [0xA336]=x<<4 and [0xA338]=y<<4. "
             "Returns cx=1 (one effect by default).",
             "VERIFIED", merge_target="combat_interaction")
def pack_spawn_pos(entry_xy_word: int):
    """[asm 8BF6] Returns (spawn_x, spawn_y) — the values written to [0xA336]/[0xA338]. The ASM also leaves
    cx=1; callers that spawn a single effect rely on that."""
    x_cell = entry_xy_word & 0xFF
    y_cell = (entry_xy_word >> 8) & 0xFF
    return (x_cell << 4) & 0xFFFF, (y_cell << 4) & 0xFFFF


@oracle_link("1030:8C13",
             "roll-bonus-sprite-id: rejection-sample rng_lcg (1030:39DF) -> v = ret & 0x7F, reroll while "
             "v >= 0x5F, return 0x2080 + v (a score/bonus-popup sprite id in [0x2080,0x20DE]). Advances the "
             "[0x2CEC..0x2CEF] generator state once per draw.",
             "ASM_MATCHED", merge_target="combat_interaction")
def roll_bonus_sprite_id(rng_state):
    """[asm 8C13] `rng_state` = (a,b,c,d) the four [0x2CEC..0x2CEF] bytes. Returns (sprite_id, new_state)."""
    a, b, c, d = rng_state
    while True:
        a, b, c, d, ret = rng_lcg(a, b, c, d)
        v = ret & 0x7F
        if v < 0x5F:
            return (0x2080 + v) & 0xFFFF, (a, b, c, d)


@oracle_link("1030:8D7B",
             "sprite-hitbox proximity/overlap test between source sprite `si` and target `di`: two coarse "
             "gates (|dX|<0x40, |dY|<0x46), then a Y-axis and X-axis AABB overlap using per-class half-extent "
             "tables [0x7190]/[0x7191] (stride 2 by id-hi&0x1F) + [0x752A], with [0xA312] selecting the full "
             "(un-halved) tolerance and [0x4F2A]/non-player gating the vertical-detail write [0xA330]/[0xA331]. "
             "Returns CF=overlap.",
             "VERIFIED", merge_target="combat_interaction")
def hitbox_overlap(rb, rw, si, di):
    """[asm 8D7B] Sprite-hitbox overlap test. ``rb``/``rw`` read a byte/word from DS; ``si``/``di`` are the
    source/target sprite-record offsets. Returns ``(hit, writes)`` — ``hit`` = the ASM's CF (True = overlap),
    ``writes`` = the ``{offset: (value, width)}`` contract (always [0xA330]; [0xA331] only when set). Pure."""
    writes: dict[int, tuple[int, int]] = {HIT_FLAG: (0, 1)}  # [asm 8D81] cleared

    # [asm 8D86/8D96] coarse box gates
    if _abs16(rw(si) - rw(di)) >= 0x40:
        return False, writes
    if _abs16(rw(si + 2) - rw(di + 2)) >= 0x46:
        return False, writes

    # [asm 8DA8] Y axis — orient so (ax, si) is the larger-Y object, (dx, di) the smaller
    ax = rw(si + 2)
    dx = rw(di + 2)
    bx = rw(si + 4)
    if _s16(ax) < _s16(dx):                       # jge keeps; else swap
        bx = rw(di + 4)
        ax, dx = dx, ax
        si, di = di, si
    idx = (bx & 0x1FFF) << 1                       # and bh,0x1F ; shl bx,1 (low byte kept)
    half_h = rb((HALF_LO + 1 + idx) & 0xFFFF)     # bl = [bx + 0x7191]
    ax = (ax - half_h) & 0xFFFF
    if _s16(ax) >= _s16(dx):                       # [asm 8DCA] jge -> no overlap
        return False, writes

    a312 = rb(PASS_FLAG)
    if a312 == 0:                                  # [asm 8DD1] jne skips the vertical-detail set
        depth = (dx - ax) & 0xFFFF                 # sub dx,ax
        do_set = False
        if _s16(rw(PLAYER_YVEL)) >= 0x80:          # [asm 8DDB] jge -> set
            do_set = True
        elif not (depth > (half_h >> 1)):          # [asm 8DDF] ja -> skip (unsigned)
            if si != PLAYER_REC:                   # [asm 8DE7] je -> skip
                do_set = True
        if do_set:
            writes[HIT_FLAG] = (1, 1)              # inc byte [0xA330]
            writes[HIT_DETAIL] = (depth, 2)        # mov word [0xA331],dx

    # [asm 8DF1] X axis — left edges = pos - X half-width; overlap if min_left + hw2 > max_left
    src_idx = (rw(si + 4) & 0x1FFF) << 1           # bp
    src_left = (rw(si) - rb((HALF_WX + src_idx) & 0xFFFF)) & 0xFFFF
    tgt_idx = (rw(di + 4) & 0x1FFF) << 1
    tgt_left = (rw(di) - rb((HALF_WX + tgt_idx) & 0xFFFF)) & 0xFFFF
    hw2 = rb((HALF_LO + tgt_idx) & 0xFFFF)         # bl = [bx + 0x7190]
    ax, dx = tgt_left, src_left
    if not (_s16(ax) < _s16(dx)):                  # [asm 8E1C] jl keeps; else swap to src's hw2
        ax, dx = dx, ax
        hw2 = rb((HALF_LO + src_idx) & 0xFFFF)
    if a312 == 0:                                  # [asm 8E2B] jne skips the halving
        hw2 >>= 1                                  # sar bx,1 (hw2 >= 0)
    ax = (ax + hw2) & 0xFFFF
    hit = _s16(ax) > _s16(dx)                       # [asm 8E33] jle -> no hit
    return hit, writes


# effect/score-burst object slots (the free pool after the 12 main objects at 0x4FD0)
BURST_SLOT_LO = 0x50A8
BURST_SLOT_HI = 0x52E8
BURST_STRIDE = 0x12
BURST_SPRITE = 0xA33A   # [0xA33A] the sprite id to spawn (set by 899E before the call)


@oracle_link("1030:8D1B",
             "score/effect-burst emitter: spawn `cx` sprites into the free object slots 0x50A8..0x52E8 "
             "(stride 0x12); each gets sprite id [0xA33A], pos [0xA336]/[0xA338], Xvel=ax, Yvel/state=dx, "
             "[+0xC]=0xC6, [+0x11]=0, [+9]=0xFFFF. After each spawn ax is negated (alternating spread) and on "
             "every even spawn ax,dx step down 0x10 (ax zeroed past the 12th).",
             "ASM_MATCHED", merge_target="combat_interaction")
def spawn_effect_burst(rb, rw, ax, dx, cx):
    """[asm 8D1B] ``ax``=initial Xvel, ``dx``=initial Yvel/state, ``cx``=count. ``rb``/``rw`` read DS.
    Returns the ``{offset: (value, width)}`` writes into the free effect slots (the spawned objects)."""
    sprite = rw(BURST_SPRITE)
    px = rw(SPAWN_X)
    py = rw(SPAWN_Y)
    ax &= 0xFFFF
    dx &= 0xFFFF
    writes: dict[int, tuple[int, int]] = {}
    di = 0
    bx = BURST_SLOT_LO
    while bx < BURST_SLOT_HI:                       # [asm 8D6F] cmp bx,0x52E8 ; jb
        if rw((bx + 4) & 0xFFFF) == 0xFFFF:        # [asm 8D25] free slot
            writes[bx + 4] = (sprite, 2)
            writes[bx + 0x11] = (0, 1)
            writes[bx + 0xC] = (0xC6, 2)
            writes[bx] = (px, 2)
            writes[bx + 2] = (py, 2)
            writes[bx + 6] = (ax, 2)
            writes[bx + 0xE] = (dx, 2)
            writes[bx + 9] = (0xFFFF, 2)
            ax = (-ax) & 0xFFFF                     # [asm 8D53] neg ax
            di += 1
            if di & 1 == 0:                         # [asm 8D56] even spawn
                if di > 0xC:                        # [asm 8D5C] jbe skips
                    ax = 0                          # [asm 8D61] xor ax,ax
                ax = (ax - 0x10) & 0xFFFF           # [asm 8D63]
                dx = (dx - 0x10) & 0xFFFF           # [asm 8D66]
            cx -= 1
            if cx == 0:                             # [asm 8D6A] je -> done
                break
        bx += BURST_STRIDE
    return writes


DEBRIS_POOL_LO = 0x5450  # the debris-element object pool (16 slots, stride 0x12)
DEBRIS_POOL_N = 0x10
SCORE_LO = 0x6C0E        # 32-bit score accumulator [0x6C0E:0x6C10]
SCORE_TABLE = 0x5CAD     # per-sprite score table base (read as DS:[(sprite-0x4A)*2 - 0x5CAD])
SPAWNED_PTR = 0xA33E     # [0xA33E] = the just-spawned element pointer (8C72 reads it back)


@oracle_link("1030:8875",
             "spawn one debris element (sprite `ax`, position from `si`) into the 0x5450 pool (16 slots, "
             "stride 0x12): set [+4]=sprite, [+0]/[+2]=pos, [+0xC]=0x2C, [0xA33E]=slot; bump the 32-bit score "
             "[0x6C0E:0x6C10] by the per-sprite value at [(sprite-0x4A)*2 - 0x5CAD] when sprite-0x4A in "
             "[0,0x10]; if `si` is an effect slot (>=0x50A8) free its back-referenced slot ([si+9]).",
             "ASM_MATCHED", merge_target="combat_interaction")
def spawn_debris_element(rb, rw, ax, si):
    """[asm 8875] Returns ``(writes, slot)`` — the ``{offset: (value, width)}`` writes and the spawned slot
    offset (or None if the pool was full). ``ax``=sprite id, ``si``=position-source record offset."""
    ax &= 0xFFFF
    si &= 0xFFFF
    writes: dict[int, tuple[int, int]] = {}

    # [asm 8879] score bump for sprite ids 0x4A..0x5A
    bx = (ax - 0x4A) & 0xFFFF
    if not (bx & 0x8000) and bx <= 0x10:          # jb (negative) / ja (>0x10) skip
        val = rw(((bx << 1) - 0x5CAD) & 0xFFFF)   # shl bx,1 ; mov bx,[bx-0x5CAD]
        total = (rw(SCORE_LO) | (rw(SCORE_LO + 2) << 16)) + val   # add [6C0E] ; adc [6C10],0
        writes[SCORE_LO] = (total & 0xFFFF, 2)
        writes[SCORE_LO + 2] = ((total >> 16) & 0xFFFF, 2)

    # [asm 8894] find a free debris slot
    slot = None
    b = DEBRIS_POOL_LO
    for _ in range(DEBRIS_POOL_N):
        if rw((b + 4) & 0xFFFF) == 0xFFFF:
            slot = b
            break
        b += 0x12

    if slot is not None:                           # [asm 88A7] fill it
        writes[slot + 4] = (ax, 2)
        writes[slot] = (rw(si), 2)
        writes[slot + 2] = (rw((si + 2) & 0xFFFF), 2)
        writes[slot + 0xC] = (0x2C, 2)
        writes[SPAWNED_PTR] = (slot, 2)            # [asm 88B9] [0xA33E]=di
        # [asm 88BD] if the pos source is an effect slot, free its back-referenced slot
        if si >= 0x50A8:
            ref = rw((si + 9) & 0xFFFF)
            if ref != 0xFFFF:
                writes[(ref + 4) & 0xFFFF] = (0xFFFF, 2)

    return writes, slot
