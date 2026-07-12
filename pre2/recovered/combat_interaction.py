"""Combat / pickup interaction island — 1030:88D7 + 899E + 8C21 (in progress).

Once per frame the main loop (~1030:021D) runs the player-and-projectile interaction pass `88D7`: for each of
the 4 projectile slots (DS:0x4F2E, stride 0x12) and then — unless [0x6BC5] (player flying) — the player sprite
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
from pre2.views.dgroup_view import DictBackend, PlayerGlobals, PlayerView

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


def _s8(v: int) -> int:
    v &= 0xFF
    return v - 0x100 if v & 0x80 else v


def _sar16(v: int, n: int) -> int:
    """Arithmetic (sign-preserving) right shift of a 16-bit value (Python ``>>`` on the signed value)."""
    return (_s16(v) >> n) & 0xFFFF


def _abs16(d: int) -> int:
    """The ASM ``jns ; neg`` absolute value of a 16-bit subtraction (0x8000 stays 0x8000)."""
    d &= 0xFFFF
    return (0x10000 - d) if (d & 0x8000) else d


def _abs8(d: int) -> int:
    d &= 0xFF
    return (0x100 - d) if (d & 0x80) else d


class _Overlay:

    _IS_DGROUP_BACKEND = True   # a dgroup-view backend: RngView/PlayerView bind straight onto it
    """A byte-level read-through write buffer over base DS memory, so a composed routine's later reads see
    its own earlier writes (the 8C72 debris loop fills the pool + scatters the enemy pos in place)."""

    __slots__ = ("_rb", "b")

    def __init__(self, rb):
        self._rb = rb
        self.b: dict[int, int] = {}

    def rb(self, o: int) -> int:
        o &= 0xFFFF
        return self.b[o] if o in self.b else self._rb(o)

    def rw(self, o: int) -> int:
        o &= 0xFFFF
        return self.rb(o) | (self.rb((o + 1) & 0xFFFF) << 8)

    def wb(self, o: int, v: int) -> None:
        self.b[o & 0xFFFF] = v & 0xFF

    def ww(self, o: int, v: int) -> None:
        v &= 0xFFFF
        self.b[o & 0xFFFF] = v & 0xFF
        self.b[(o + 1) & 0xFFFF] = (v >> 8) & 0xFF

    def apply(self, writes: dict) -> None:
        for off, (val, width) in writes.items():
            (self.ww if width == 2 else self.wb)(off, val)

    def merge_bytes(self, byte_writes: dict) -> None:
        """Merge a byte-level {offset: value} dict (e.g. another routine's overlay buffer)."""
        for off, val in byte_writes.items():
            self.b[off & 0xFFFF] = val & 0xFF


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

def roll_bonus_sprite(rng) -> int:
    """:func:`roll_bonus_sprite_id` over a bound :class:`~pre2.views.dgroup_view.RngView` — reads and writes
    the LCG state through the view's backend (bind to a read-through overlay when rolling repeatedly)."""
    sid, (a, b, c, d) = roll_bonus_sprite_id((rng.lcg_a, rng.lcg_b, rng.lcg_c, rng.lcg_d))
    rng.lcg_a = a
    rng.lcg_b = b
    rng.lcg_c = c
    rng.lcg_d = d
    return sid

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


DEATH_ANIM_MARKER = 0x7D00  # the anim-script word that marks the death sequence


@oracle_link("1030:80CB",
             "advance-to-death-anim: scan the enemy's anim script forward from [di+0xC] by 2 bytes at a time "
             "until the word 0x7D00 (the death-sequence marker), then step 2 past it and store back to "
             "[di+0xC]. Returns the new [di+0xC].",
             "ASM_MATCHED", merge_target="combat_interaction")
def advance_death_anim(rw, di):
    """[asm 80CB] ``rw`` reads a DS word; ``di`` is the enemy slot. Returns the new ``[di+0xC]`` value."""
    si = rw((di + 0xC) & 0xFFFF)
    for _ in range(0x4000):                        # guard (the script always has a 0x7D00)
        si = (si + 2) & 0xFFFF
        if rw(si) == DEATH_ANIM_MARKER:
            return (si + 2) & 0xFFFF
    raise ValueError("80CB: no 0x7D00 death marker in anim script")


DEBRIS_COUNT_TABLE = 0x5C0F  # death-debris count, read as DS:[((di+0x10)>>3 & 7) - 0x5C0F]
DEF_FLAGS = 4                # [def+4] flag byte (bit0 selects launch vs bonus death)
DAMAGE = 0x7B19             # [0x7B19] current weapon damage
DEATH_BONUS_SPRITE = 0x2046  # the death-bonus sprite spawned on the non-launch path


@oracle_link("1030:8C72",
             "enemy-death handler: spawn `count` debris elements (8875) scattering the death point "
             "(pos +=9/+7, [elem+0xC] staggered by remaining*4), restore the enemy pos, mark it dead "
             "([di+0xE]=0xFF); then if [def+4]&1 the launch path (80CB death-anim, conditional [def+4] bit3 "
             "clear, knockback [di+0xA]=-min(dmg,0x19)*8 / [di+8]=+/-half), else spawn 6 death-bonus sprites "
             "(8D1B, id 0x2046) at the enemy pos. Composes the verified leaves over a read-through overlay.",
             "ASM_MATCHED", merge_target="combat_interaction")
def death_handler(rb, rw, bx, di, src_si):
    """[asm 8C72] ``bx``=enemy def-ptr, ``di``=enemy slot, ``src_si``=the attacker's sprite record (the
    caller's si — restored by the ASM's final ``pop si``, used for the knockback facing). Returns the
    byte-level ``{offset: value}`` write contract. ``rb``/``rw`` read base DS; all mutation is staged on an
    overlay so the composed leaves (8875/8D1B/80CB) see each other's writes."""
    ov = _Overlay(rb)
    sprite = (_s8(rb((bx + 8) & 0xFFFF)) + 0x4A) & 0xFFFF      # [asm 8C72] [def+8] signed + 0x4A
    cnt_idx = (rb((di + 0x10) & 0xFFFF) >> 3) & 7              # [asm 8C7A]
    count = rb((cnt_idx - DEBRIS_COUNT_TABLE) & 0xFFFF)

    enemy = di                                                 # [asm 8C8E] si = enemy slot (the debris source)
    orig_x = ov.rw(enemy)                                      # [asm 8C90/8C92] saved pos (restored later)
    orig_y = ov.rw((enemy + 2) & 0xFFFF)

    rem = count
    while rem != 0:                                            # [asm 8C96] debris loop
        w, slot = spawn_debris_element(ov.rb, ov.rw, sprite, enemy)
        ov.apply(w)
        if slot is not None:
            elem = ov.rw(SPAWNED_PTR)                          # [asm 8C99] di = [0xA33E]
            cur = ov.rw((elem + 0xC) & 0xFFFF)                 # [asm 8CA2] [elem+0xC] -= rem*4
            ov.ww((elem + 0xC) & 0xFFFF, (cur - (rem << 2)) & 0xFFFF)
        ov.ww((enemy + 2) & 0xFFFF, (ov.rw((enemy + 2) & 0xFFFF) + 7) & 0xFFFF)  # [asm 8CA6] scatter
        ov.ww(enemy, (ov.rw(enemy) + 9) & 0xFFFF)                               # [asm 8CAA]
        rem = (rem - 1) & 0xFFFF

    ov.ww((enemy + 2) & 0xFFFF, orig_y)                       # [asm 8CB1/8CB4] restore enemy pos
    ov.ww(enemy, orig_x)
    ov.wb((di + 0xE) & 0xFFFF, 0xFF)                          # [asm 8CB7] mark dead

    def_flags = ov.rb((bx + DEF_FLAGS) & 0xFFFF)
    if def_flags & 1:                                          # [asm 8CBB] jne -> launch path
        ov.ww((di + 0xC) & 0xFFFF, advance_death_anim(ov.rw, di))   # [asm 8CE1] 80CB
        if (def_flags & 0xC8) != 0x88:                        # [asm 8CE4-8CF2] conditional bit3 clear
            ov.wb((bx + DEF_FLAGS) & 0xFFFF, def_flags & 0xF7)
        dmg = ov.rb(DAMAGE)                                   # [asm 8CF5]
        if dmg > 0x19:
            dmg = 0x19
        yvel = ((-dmg) << 3) & 0xFFFF                          # [asm 8D02-8D08] -min(dmg,0x19)*8
        ov.ww((di + 0xA) & 0xFFFF, yvel)
        xvel = _sar16(yvel, 1)                                 # [asm 8D0D] ax sar 1
        if not (ov.rb((src_si + 5) & 0xFFFF) & 0x80):         # [asm 8D0F] test attacker [si+5],0x80 ; jne keep
            xvel = (-xvel) & 0xFFFF
        ov.ww((di + 8) & 0xFFFF, xvel)
    else:                                                      # [asm 8CC1] bonus path
        ov.ww(SPAWN_X, ov.rw(di))                             # [0xA336] = enemy X
        ov.ww(SPAWN_Y, ov.rw((di + 2) & 0xFFFF))             # [0xA338] = enemy Y
        ov.ww(BURST_SPRITE, DEATH_BONUS_SPRITE)              # [0xA33A] = 0x2046
        ov.apply(spawn_effect_burst(ov.rb, ov.rw, 0x30, 0xFF80, 6))   # [asm 8CDC] 8D1B

    return ov.b


ENEMY_SLOTS_LO = 0x4FD0   # the 12 active object/enemy slots (stride 0x12)
ENEMY_SLOTS_N = 0x0C
DEF_NONCOLLIDE = 0x10     # [def+4] bit4 -> the object ignores projectile/player collision
KILL_SFX = 2              # play_sfx index on a kill


def projectile_vs_enemies(rb, rw, si):
    """[asm 8C21] Scan the 12 enemy slots (0x4FD0) for the first the source sprite ``si`` (a projectile or
    the player) overlaps; on a hit mark + damage it, kill (SFX + death_handler) or knock it back, and consume
    the source. Returns ``(writes, sfx, hit, slot)`` — ``writes`` is the byte-level ``{offset: value}``
    contract, ``sfx`` the SFX indices to emit, ``hit`` the ASM's CF, ``slot`` the enemy hit (or None)."""
    damage = rb(DAMAGE)
    writes: dict[int, int] = {}
    sfx: list[int] = []

    di = ENEMY_SLOTS_LO
    for _ in range(ENEMY_SLOTS_N):
        if rw((di + 4) & 0xFFFF) != 0xFFFF and rb((di + 0xE) & 0xFFFF) != 0xFF:  # [asm 8C27/8C2D]
            bx = rw((di + 6) & 0xFFFF)
            if not (rb((bx + 4) & 0xFFFF) & DEF_NONCOLLIDE):                     # [asm 8C36] collidable
                hit, hb = hitbox_overlap(rb, rw, si, di)                         # [asm 8C3C] 8D7B
                for off, (val, width) in hb.items():                            # 8D7B's [0xA330]/[0xA331]
                    writes[off & 0xFFFF] = val & 0xFF
                    if width == 2:
                        writes[(off + 1) & 0xFFFF] = (val >> 8) & 0xFF
                if hit:                                                          # [asm 8C3F] jae skips
                    writes[(di + 5) & 0xFFFF] = rb((di + 5) & 0xFFFF) | 0x40     # [asm 8C41] mark hit
                    hp = rb((di + 0xF) & 0xFFFF)
                    writes[(di + 0xF) & 0xFFFF] = (hp - damage) & 0xFF           # [asm 8C48] HP -= damage
                    if hp < damage:                                             # [asm 8C4B] borrow -> kill
                        sfx.append(KILL_SFX)                                     # [asm 8C50] play_sfx(2)
                        for off, val in death_handler(rb, rw, bx, di, si).items():
                            writes[off & 0xFFFF] = val & 0xFF
                    else:                                                        # [asm 8C58] knockback
                        kb = _s16(rw((di + 8) & 0xFFFF)) >> 2                     # ax = [di+8] >> 2 (arith)
                        nx = (rw(di) - kb) & 0xFFFF
                        writes[di] = nx & 0xFF
                        writes[(di + 1) & 0xFFFF] = (nx >> 8) & 0xFF
                    cons = (si + 4) & 0xFFFF                                      # [asm 8C61] consume source
                    writes[cons] = 0xFF
                    writes[(cons + 1) & 0xFFFF] = 0xFF
                    return writes, sfx, True, di
        di = (di + 0x12) & 0xFFFF

    return writes, sfx, False, None


SPARKLE_RING_PTR = 0x6BBE  # write pointer into the effect/sparkle ring [0x4F76..0x4FBE]
SPARKLE_RING_LO = 0x4F76
SPARKLE_RING_TOP = 0x4FBE   # wrap target (the ring grows downward, stride 0x12)
SPARKLE_SPRITE = 0x35


@oracle_link("1030:5E41",
             "spawn a pickup-sparkle effect (sprite 0x35) at (ax, dx) into the downward-growing ring at "
             "[0x4F76..0x4FBE] (stride 0x12): write [ptr]=X, [ptr+2]=Y, [ptr+4]=0x35, then advance the pointer "
             "[0x6BBE] down 0x12, wrapping to 0x4FBE when it would drop below 0x4F76.",
             "ASM_MATCHED", merge_target="combat_interaction")
def spawn_pickup_sparkle(rw, ax, dx):
    """[asm 5E41] ``rw`` reads a DS word. ``ax``/``dx`` = the effect X/Y. Returns the ``{offset: (value,
    width)}`` writes (the ring record + the advanced [0x6BBE] pointer)."""
    bx = rw(SPARKLE_RING_PTR)
    writes = {
        bx & 0xFFFF: (ax & 0xFFFF, 2),                  # [asm 5E46] [bx]   = X
        (bx + 2) & 0xFFFF: (dx & 0xFFFF, 2),            # [asm 5E48] [bx+2] = Y
        (bx + 4) & 0xFFFF: (SPARKLE_SPRITE, 2),         # [asm 5E4B] [bx+4] = sprite id
    }
    bx = (bx - 0x12) & 0xFFFF                            # [asm 5E50]
    if bx < SPARKLE_RING_LO:                             # [asm 5E53] jae keeps; else wrap
        bx = SPARKLE_RING_TOP
    writes[SPARKLE_RING_PTR] = (bx, 2)                   # [asm 5E5C] new pointer
    return writes


COLLECTED_COUNTER = 0x2A76  # bumped once per bonus collected
MAP_SEG_PTR = 0x2DDA        # [0x2DDA] holds the level-map segment (the es for the tile write)
REDRAW_DIRTY = (0x6BBD, 0x2DF4, 0x2DE0)  # set when the consumed tile is redrawn on-screen


@oracle_link("1030:8B6E",
             "bonus collect tail (the 8B77 body, reached by jmp from 8A5A): bump the collected counter "
             "[0x2A76], clear the cell ([di+3]=0xFFFF), restore the underlying tile id [di+1] into the level "
             "map (es=[0x2DDA]) at the cell's map offset (the old [di+3]); if that cell is on-screen, set the "
             "redraw-dirty flags [0x6BBD]/[0x2DF4]/[0x2DE0] (the actual 453B+3B77 tile blit is a render "
             "side-effect the live hook performs). Returns CF=1.",
             "ASM_MATCHED", merge_target="combat_interaction")
def bonus_collect_tail(rb, rw, di):
    """[asm 8B6E/8B77] Returns ``(ds_writes, map_writes, onscreen)`` — ``ds_writes`` the DS ``{offset:
    (value, width)}`` contract, ``map_writes`` the ``{offset: (value, width)}`` writes into the level-map
    segment (es=[0x2DDA]), and ``onscreen`` whether the consumed tile must be re-blitted."""
    ds = {COLLECTED_COUNTER: ((rw(COLLECTED_COUNTER) + 1) & 0xFFFF, 2)}   # [asm 8B77] inc [0x2A76]
    old = rw((di + 3) & 0xFFFF)                          # [asm 8B7E] old [di+3] = the map offset
    ds[(di + 3) & 0xFFFF] = (0xFFFF, 2)                  # clear the cell
    tile = rb((di + 1) & 0xFFFF)                         # [asm 8B81] tile id to restore
    map_writes = {old & 0xFFFF: (tile, 1)}              # [asm 8B8A] level_map[old] = tile

    al = old & 0xFF                                      # map X cell
    ah = (old >> 8) & 0xFF                               # map Y cell
    g = PlayerGlobals(DictBackend(rb, lambda o: rb(o) | (rb((o + 1) & 0xFFFF) << 8)))
    cam_x = g.cam_col
    cam_y = g.cam_row
    sx = (al - cam_x) & 0xFF                             # [asm 8B8D] sub al,[0x2DE4]
    sy = (ah - cam_y) & 0xFF                             # [asm 8B97] sub ah,[0x2DE6]
    onscreen = (al >= cam_x and sx < 0x14 and ah >= cam_y and sy < 0x0C)
    if onscreen:                                         # [asm 8BD8-8BE2] redraw-dirty flags
        ds[0x6BBD] = (1, 1)
        ds[0x2DF4] = (1, 1)
        ds[0x2DE0] = (0x55AA, 2)
    return ds, map_writes, onscreen


RNG_LCG = 0x2CEC          # rng_lcg state: a/b/c bytes [0x2CEC..0x2CEE], d = WORD [0x2CEF]
BONUS_DEBOUNCE = 0xA33C   # last-collect frame timestamp ([0x6BD5])
LEVEL_ID = 0x2D8A
PLAYER_STRUCT = 0x4F1C


def _ov_rng(ov):
    """Advance the LCG over the overlay state; return new b. (= RngView.roll — the named-state form.)"""
    from pre2.views.dgroup_view import RngView
    return RngView(ov).roll()


def _bonus_popup_cx(lvl):
    """[asm 8ADF-8AF4 / 8B19-8B2E] the level-dependent score-popup sprite id."""
    if lvl == 3:
        return 0x132
    if lvl in (6, 7):
        return 0x12C
    return 0x134


def _bonus_facing_xvel(ov, src_si):
    """[asm 8B46-8B5C] popup Xvel = 0x30, negated unless the source's facing value is negative."""
    if src_si >= PLAYER_STRUCT:
        v = ov.rw((src_si + 6) & 0xFFFF)
    else:
        v = _s16(PlayerView(ov).facing)
    return 0x30 if _s16(v) < 0 else (-0x30) & 0xFFFF   # jl keeps 0x30; else neg


def _bonus_finish(ov, di):
    """[asm 8B66] decrement the cell counter; collect (8B77) on underflow. Returns (map_writes, onscreen,
    collected)."""
    cnt = ov.rb((di + 2) & 0xFFFF)
    ov.wb((di + 2) & 0xFFFF, (cnt - 1) & 0xFF)
    if cnt == 0:                                          # sub borrow -> collect
        ds_c, map_c, onscreen = bonus_collect_tail(ov.rb, ov.rw, di)
        ov.apply(ds_c)
        return map_c, onscreen, True
    return {}, False, False


@oracle_link("1030:8A5A",
             "bonus hit handler: spawn a pickup sparkle (5E41) at the cell; then a counter (bit7) path "
             "(dec [cell+2]; on underflow collect, else a random-id sparkle + collect), or the normal path "
             "with a frame debounce ([0xA33C] vs [0x6BD5], <6 -> ignore CF=0), level-dependent ([0x2D8A]) "
             "score-popup bursts (8D1B) keyed on [cell+2]&0x40, then decrement+collect (8B6E) on underflow. "
             "Returns CF = real collect.",
             "ASM_MATCHED", merge_target="combat_interaction")
def bonus_hit_handler(rb, rw, di, src_si):
    """[asm 8A5A] ``di`` = the bonus-cell record, ``src_si`` = the source sprite (for the popup facing).
    Returns ``(ds_writes, map_writes, onscreen, collected)`` — the byte-level DS contract, the level-map
    writes (es=[0x2DDA]), whether the consumed tile needs an on-screen re-blit, and the ASM's CF."""
    ov = _Overlay(rb)
    # [8A64] sparkle at the cell centre (x*16+8, y*16+0xC)
    cell = ov.rw((di + 3) & 0xFFFF)
    ov.apply(spawn_pickup_sparkle(
        ov.rw, (((cell & 0xFF) << 4) + 8) & 0xFFFF, ((((cell >> 8) & 0xFF) << 4) + 0xC) & 0xFFFF))

    t = ov.rb((di + 2) & 0xFFFF)
    if t & 0x80:                                          # [8A83] counter bonus
        newt = (t - 1) & 0xFF
        ov.wb((di + 2) & 0xFFFF, newt)
        if newt & 0x80:                                  # underflow -> stc, no collect
            return ov.b, {}, False, True
        px, py = pack_spawn_pos(ov.rw((di + 3) & 0xFFFF))   # [8A8D] 8BF6
        ov.ww(SPAWN_X, px)
        ov.ww(SPAWN_Y, py)
        r = _ov_rng(ov) & 7                              # [8A90] reroll until nonzero
        while r == 0:
            r = _ov_rng(ov) & 7
        ov.ww(BURST_SPRITE, (r + 0x6E) & 0xFFFF)
        ov.ww(SPAWN_Y, (ov.rw(SPAWN_Y) - 0x70) & 0xFFFF)
        ov.apply(spawn_effect_burst(ov.rb, ov.rw, 0, 0, 1))   # [8AA9] 8D1B
        ds_c, map_c, onscreen = bonus_collect_tail(ov.rb, ov.rw, di)   # [8AAE] collect
        ov.apply(ds_c)
        return ov.b, map_c, onscreen, True

    # [8AB1] normal bonus: frame debounce
    frame = PlayerGlobals(ov).frame_stamp
    if _abs16(frame - ov.rw(BONUS_DEBOUNCE)) < 6:
        return ov.b, {}, False, False                    # recently collected -> ignore
    ov.ww(BONUS_DEBOUNCE, frame)
    px, py = pack_spawn_pos(ov.rw((di + 3) & 0xFFFF))   # [8ACC] 8BF6
    ov.ww(SPAWN_X, px)
    ov.ww(SPAWN_Y, py)
    lvl = ov.rb(LEVEL_ID)
    t2 = ov.rb((di + 2) & 0xFFFF)

    if not (t2 & 0x40):                                  # [8B40] regular bonus: random sprite (8C13)
        r = _ov_rng(ov) & 0x7F
        while r >= 0x5F:
            r = _ov_rng(ov) & 0x7F
        ov.ww(BURST_SPRITE, (0x2080 + r) & 0xFFFF)
        ov.apply(spawn_effect_burst(ov.rb, ov.rw, _bonus_facing_xvel(ov, src_si), 0xFF90, 1))
    elif t2 == 0x40:                                     # [8AD5] two-burst bonus
        ov.wb((di + 2) & 0xFFFF, 0)
        ov.ww(BURST_SPRITE, _bonus_popup_cx(lvl))
        ov.apply(spawn_effect_burst(ov.rb, ov.rw, 0x20, 0xFFD0, 2))   # [8B04]
        ov.ww(BURST_SPRITE, 0x136 if lvl == 8 else 0xE5)             # [8B07]
        ov.apply(spawn_effect_burst(ov.rb, ov.rw, _bonus_facing_xvel(ov, src_si),
                                    0xFF90, 2 if lvl == 8 else 4))    # [8B61]
    else:                                                # [8B19] bit6 set, single burst, no facing
        ov.ww(BURST_SPRITE, _bonus_popup_cx(lvl))
        ov.apply(spawn_effect_burst(ov.rb, ov.rw, 0x30, 0xFFA0, 4))

    map_writes, onscreen, collected = _bonus_finish(ov, di)
    return ov.b, map_writes, onscreen, collected


BONUS_CELL_LIST = 0x8C8D  # 80 bonus cells, stride 5 ([+3] = packed x/y map offset)
BONUS_CELL_N = 0x50
DEDUP_BUF = 0xA2A8        # flood-fill dedup buffer (one byte per cell index)


def bonus_pickup_scan(rb, rw, si):
    """[asm 899E] Scan the 80-cell bonus list (0x8C8D) for cells near the source sprite ``si``; call
    bonus_hit_handler for each in range; on the first that collects, flood-fill-collect all connected cells
    (8-adjacency in cell coords, deduped via 0xA2A8). Returns ``(ds_writes, map_writes, redraws, hit)`` —
    the byte-level DS contract, the level-map ``{offset: (value, width)}`` writes, the list of on-screen
    map offsets whose tile must be re-blitted (render side-effect for the live hook), and the ASM's CF."""
    ov = _Overlay(rb)
    src_x_lo = (_s16(ov.rw(si)) >> 4) & 0xFF              # [asm 89A4] (ax = [si]>>4); only the low byte used
    bp_y = (ov.rw((si + 2) & 0xFFFF) - 0x10) & 0xFFFF     # [asm 89A8] bp = [si+2] - 0x10
    map_all: dict[int, tuple[int, int]] = {}
    redraws: list[int] = []

    di = BONUS_CELL_LIST
    for _ in range(BONUS_CELL_N):
        cell = ov.rw((di + 3) & 0xFFFF)
        if cell != 0xFFFF \
                and _abs8((cell & 0xFF) - src_x_lo) <= 1 \
                and _abs16(((cell >> 8) & 0xFF) * 16 - bp_y) < 0x10:   # [asm 89BC/89CC] in range
            ov.ww((si + 4) & 0xFFFF, 0xFFFF)             # [asm 89E3] consume the source
            ds_h, map_h, onscr_h, collected = bonus_hit_handler(ov.rb, ov.rw, di, si)   # [asm 89EB] 8A5A
            ov.merge_bytes(ds_h)
            map_all.update(map_h)
            if onscr_h:
                redraws.append(cell)                       # the original cell map offset (key in map_writes)
            if collected:                                 # [asm 89EE] CF=1 -> flood-fill the rest
                self_map, self_redraws = _flood_collect(ov, cell)
                map_all.update(self_map)
                redraws.extend(self_redraws)
                return ov.b, map_all, redraws, True
        di = (di + 5) & 0xFFFF

    return ov.b, map_all, redraws, False                  # [asm 8A57] clc


def _flood_collect(ov, center):
    """[asm 89F0-8A4B] Flood-fill collect of cells connected (8-adjacency in cell coords) to ``center``,
    deduped by cell index in the 0xA2A8 buffer. Returns (map_writes, redraws)."""
    for k in range(BONUS_CELL_N):                         # [asm 89F4] clear dedup buffer
        ov.wb((DEDUP_BUF + k) & 0xFFFF, 0)
    map_all: dict[int, tuple[int, int]] = {}
    redraws: list[int] = []
    stack = [center]
    while stack:                                          # [asm 8A44] pop a center, re-scan
        cur = stack.pop()
        si = BONUS_CELL_LIST
        for idx in range(BONUS_CELL_N):                   # [asm 8A06] scan all cells
            ax = ov.rw((si + 3) & 0xFFFF)
            if ax != 0xFFFF and ax != cur \
                    and _abs8((ax & 0xFF) - (cur & 0xFF)) <= 1 \
                    and _abs8(((ax >> 8) & 0xFF) - ((cur >> 8) & 0xFF)) <= 1 \
                    and ov.rb((DEDUP_BUF + idx) & 0xFFFF) == 0:   # [asm 8A27] dedup
                ov.wb((DEDUP_BUF + idx) & 0xFFFF, 1)
                stack.append(ax)                          # [asm 8A37] push as a new center
                ds_c, map_c, onscreen = bonus_collect_tail(ov.rb, ov.rw, si)   # [asm 8A3C] 8B6E
                ov.apply(ds_c)
                map_all.update(map_c)
                if onscreen:
                    redraws.append(ax)
            si = (si + 5) & 0xFFFF
    return map_all, redraws
