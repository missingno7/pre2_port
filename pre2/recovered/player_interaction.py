"""The player<->world interaction pass (1030:8295..8617) — main-loop subsystem (called at 0x0232).

Two loops over the player (0x4f1c): loop1 = player-vs-enemy collision (stomp / hurt / die), loop2 = player-vs
-entity pickup/powerup dispatch (~25 id handlers). Both reuse the combat island's recovered ``hitbox_overlap``
+ ``death_handler``. This module recovers the island bottom-up; each block is annotated with its ``[asm
<offset>]`` origin and proven byte-exact in shadow (pre2/probes/probe_player_interaction.py).

Recovered so far (the shared keystones used by both loops + the loop2 handlers):
- ``spawn_pickup_effect`` (8875) — add score + spawn a popup/sparkle effect, consume a linked entity.
- ``advance_anim_script`` (80CB) — advance an object's anim-script to its next section (the stomp/dying anim).
"""
from __future__ import annotations

from pre2.recovered.combat_interaction import death_handler, hitbox_overlap
from pre2.recovered.player_collision import _offcamera_trigger

SCORE = 0x6C0E              # [asm 888B] 32-bit player score (low 0x6C0E, high 0x6C10)
SCORE_TABLE = 0xA353        # [asm 8887] score values, indexed ((id-0x4a)<<1); = (-0x5CAD)&0xFFFF
EFFECT_LIST = 0x5450        # [asm 8897] 16-slot popup/effect spawn list, stride 0x12
EFFECT_LIST_COUNT = 0x10
EFFECT_PTR = 0xA33E         # [asm 88B9] [0xA33E] = the last spawned effect slot
ENTITY_LIST_START = 0x50A8  # [asm 88BD] si >= this => a loop2 entity (consume its [+9] linked entity)
ANIM_SECTION_MARKER = 0x7D00


def spawn_pickup_effect(rb, rw, eff_id: int, src_si: int) -> dict:
    """Recover ``1030:8875`` — feedback for collecting/hitting something: add score for a collectible id and
    spawn a popup/sparkle effect entity at the source position; for an entity pickup (``src_si`` in the
    0x50A8 list) also consume its linked entity ``[src_si+9]``. ``eff_id`` = the effect/sprite id (``ax`` at
    the call), ``src_si`` = the source object/entity. Returns the DS ``{offset:(value,width)}`` writes."""
    out: dict = {}
    # [887B] score-add for collectible ids 0x4A..0x5A (sub bx,0x4a; jb skip; cmp bx,0x10; ja skip)
    bx = (eff_id - 0x4A) & 0xFFFF
    if bx <= 0x10:
        val = rw((SCORE_TABLE + (bx << 1)) & 0xFFFF)            # [8887] table word
        score = (rw(SCORE) | (rw((SCORE + 2) & 0xFFFF) << 16)) + val   # [888B/888F] 32-bit add
        out[SCORE] = (score & 0xFFFF, 2)
        out[(SCORE + 2) & 0xFFFF] = ((score >> 16) & 0xFFFF, 2)
    # [8894] allocate a free effect slot ([+4]==0xFFFF)
    slot = None
    for k in range(EFFECT_LIST_COUNT):
        base = (EFFECT_LIST + k * 0x12) & 0xFFFF
        if rw((base + 4) & 0xFFFF) == 0xFFFF:
            slot = base
            break
    if slot is None:                                           # [88A5] no slot -> only the score landed
        return out
    out[(slot + 4) & 0xFFFF] = (eff_id & 0xFFFF, 2)            # [88A7] effect id
    out[slot] = (rw(src_si & 0xFFFF), 2)                       # [88AA] X = [src]
    out[(slot + 2) & 0xFFFF] = (rw((src_si + 2) & 0xFFFF), 2)  # [88AE] Y = [src+2]
    out[(slot + 0xC) & 0xFFFF] = (0x2C, 2)                     # [88B4]
    out[EFFECT_PTR] = (slot, 2)                                # [88B9]
    if (src_si & 0xFFFF) >= ENTITY_LIST_START:                 # [88BD] loop2 entity -> consume the link
        link = rw((src_si + 9) & 0xFFFF)
        if link != 0xFFFF:                                     # [88C7]
            out[(link + 4) & 0xFFFF] = (0xFFFF, 2)             # [88CC]
    return out


def advance_anim_script(rw, di: int) -> dict:
    """Recover ``1030:80CB`` — advance object ``[di]``'s anim-script pointer ``[di+0xC]`` past the next
    ``0x7D00`` section marker (used on a stomp to switch the enemy to its squashed/dying animation)."""
    si = rw((di + 0xC) & 0xFFFF)
    while True:                                                # [80CF] si += 2 until [si]==0x7D00
        si = (si + 2) & 0xFFFF
        if rw(si) == ANIM_SECTION_MARKER:
            break
    si = (si + 2) & 0xFFFF                                     # [80D7] past the marker
    return {(di + 0xC) & 0xFFFF: (si, 2)}


# --- loop1: player-vs-enemy collision (829F..83D4) — stomp / hurt / die ---------------------------------
OBJ_BASE = 0x4FD0          # the 12-slot object list
PLAYER = 0x4F1C            # player X (si in loop1)
PLAYER_Y = 0x4F1E
PLAYER_YVEL = 0x4F2A
PLAYER_DEATH = 0x4F2D      # death-state byte (0 = alive)
KNOCKBACK_Y = 0xA331       # [0xA331] hurt/knockback Y delta
HURT_SFX_TABLE = 0xA3E5    # [bx-0x5C1B] escalating hurt-effect ids
_INSTADEATH = 0x6BE2       # [0x6BE2]!=0 -> touching an enemy = instant death_handler
_DEATH_FLAG_A330 = 0xA330  # [0xA330]!=0 -> die on touch
_DEATH_FLAG_4F2B = 0x4F2B  # [0x4F2B]<0 (signed byte) -> die on touch
_ATTACK = 0x6BC7           # [0x6BC7]!=0 -> player attacking/invulnerable (can stomp)


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _knockback(rb, rw, yvel: int) -> dict:
    """[asm 837A] knock the player up: Yvel=yvel, clear [0x6BD2], player Y -= [0xA331]."""
    return {PLAYER_YVEL: (yvel & 0xFFFF, 2), 0x6BD2: (0, 1),
            PLAYER_Y: ((rw(PLAYER_Y) - rw(KNOCKBACK_Y)) & 0xFFFF, 2)}


def _hurt(rb, rw, di):
    """[asm 8348] player hurt by object ``di``: hurt sfx (3) + an escalating hit-counter effect + knockback.
    Returns ``(writes, sfx)``."""
    out = {}
    sfx = [3]                                              # [834B] play_sfx(3)
    hc = rb((di + 0x10) & 0xFFFF) >> 2                     # [834E] hit count
    if hc != 0xB:                                          # [8355] cap
        out[(di + 0x10) & 0xFFFF] = ((rb((di + 0x10) & 0xFFFF) + 4) & 0xFF, 1)   # [8359]
        cnt = (hc + 1) & 0xFF                              # [835D] inc
        if (cnt & 1) == 0:                                 # [8360] shr/jnb -> only on even counts
            eff = rw((HURT_SFX_TABLE + cnt) & 0xFFFF)      # [8366]
            out.update(spawn_pickup_effect(rb, rw, eff, PLAYER))   # [836A]
    yvel = 0xFF20 if rb(0x27EA) != 0 else 0xFFC0           # [836D/8377]
    out.update(_knockback(rb, rw, yvel))
    return out, sfx


def _death(rb, rw, di):
    """[asm 838A] player killed by object ``di``: death sfx (9) + death-state + off-camera respawn trigger."""
    out = {}
    sfx = [9]                                              # [838D] play_sfx(9)
    defp = rw((di + 6) & 0xFFFF)                           # [8390] di = [di+6] (the type def)
    out[(defp + 4) & 0xFFFF] = (rb((defp + 4) & 0xFFFF) & 0xFE, 1)   # [8393]
    out[PLAYER_DEATH] = (0x2C, 1)                          # [8397]
    out[0x6BD0] = (0, 1)                                   # [839C]
    out[PLAYER_YVEL] = (0xFF80, 2)                         # [83A1]
    out[0x4F22] = ((-((rw(0x4F22) << 2) & 0xFFFF)) & 0xFFFF, 2)   # [83A7] = -([0x4F22]<<2)
    out[_ATTACK] = (0, 1)                                  # [83B3]
    old = rb(0x6BC5)                                       # [83B8] cmp before the clear
    out[0x6BC5] = (0, 1)                                   # [83BD]
    if old == 0:                                           # [83C2] jne skip
        n = (rb(0x27D6) - 1) & 0xFF                        # [83C4] dec
        out[0x27D6] = (n, 1)
        if n & 0x80:                                       # [83C8] jns skip -> call only if went negative
            out.update(_offcamera_trigger(rb))             # [83CA] 65B3
    return out, sfx


def _stomp(rb, rw, di):
    """[asm 82F7] player stomps object ``di`` (attacking + falling fast): spawn effect, mark stomped, and on
    the 3rd stomp (``[di+0x10]&3 == 2``) kill it (squash anim + bounce velocities); else knock the player up."""
    out = {}
    v10 = rb((di + 0x10) & 0xFFFF)
    dl = v10 & 3
    out.update(spawn_pickup_effect(rb, rw, ((dl << 1) + 0x52) & 0xFFFF, PLAYER))   # [82F7..8304]
    out[(di + 5) & 0xFFFF] = (rb((di + 5) & 0xFFFF) | 0x40, 1)    # [8307]
    if dl == 2:                                            # [830B] kill
        out[(di + 0xE) & 0xFFFF] = (0xFF, 1)               # [8310]
        out.update(advance_anim_script(rw, di))            # [8314] 80CB
        defp = rw((di + 6) & 0xFFFF)
        out[(defp + 4) & 0xFFFF] = (rb((defp + 4) & 0xFFFF) & 0xF7, 1)   # [831A]
        out[(di + 0xA) & 0xFFFF] = (0xFF38, 2)             # [831E] object Yvel up
        ax = abs(_s16(rw(PLAYER_YVEL)))                    # [8323] |player Yvel|
        if not (_s16(rw(di)) > _s16(rw(PLAYER))):          # [8330] obj left of player -> push left
            ax = -ax
        out[(di + 8) & 0xFFFF] = ((ax * 3) & 0xFFFF, 2)    # [8336] object Xvel = 3*(+/-|Yvel|)
        return out, []
    out[(di + 0x10) & 0xFFFF] = ((v10 + 1) & 0xFF, 1)      # [8340]
    out.update(_knockback(rb, rw, 0xFFA0))                 # [8343] ax=0xffa0 -> 837A
    return out, []


def _loop1_hit_outcome(rb, rw, di):
    """[asm 82C8..82F7] dispatch a player-vs-object hit (instant-death case handled by the walk). Returns
    ``(writes, sfx)``."""
    if rb(_DEATH_FLAG_A330) != 0 or (rb(_DEATH_FLAG_4F2B) & 0x80):    # [82D5/82DF] die
        return _death(rb, rw, di)
    if rb(_ATTACK) == 0:                                              # [82E9] hurt
        return _hurt(rb, rw, di)
    if _s16(rw(PLAYER_YVEL)) <= 0x20:                                 # [82F0] not falling -> bump
        return _knockback(rb, rw, 0xFFA0), []
    return _stomp(rb, rw, di)                                         # [82F7] stomp


def loop1(rb, rw, apply, emit_sfx):
    """[asm 829F..83D4] walk the 12 object slots vs the player; on the first qualifying overlap, an
    instant-death object runs ``death_handler`` and the walk CONTINUES, any other outcome
    (death/hurt/stomp/bump) applies + returns. ``apply({off:(val,width)})`` commits writes (so a later
    spawn's find-free sees earlier ones); ``emit_sfx(idx)`` plays a sound. Returns ``early_ret`` — True means
    the 8295 routine returns here (loop2 is skipped)."""
    if rb(PLAYER_DEATH) != 0:                              # [8295] already dying -> straight to loop2
        return False
    di = OBJ_BASE
    for _ in range(12):                                    # [82A5] cx=0xC
        defp = rw((di + 6) & 0xFFFF)
        if (rw((di + 4) & 0xFFFF) != 0xFFFF and (rb((di + 5) & 0xFFFF) & 0x20)   # [82A8..82B2]
                and not (rb((defp + 4) & 0xFFFF) & 0x10)   # [82B7]
                and rb((di + 0xE) & 0xFFFF) != 0xFF):      # [82BD]
            hit, hb = hitbox_overlap(rb, rw, PLAYER, di)   # [82C3] 8D7B
            apply(hb)
            if hit:
                if rw(_INSTADEATH) != 0:                   # [82C8] instant death_handler, keep walking
                    apply(death_handler(rb, rw, defp, di, PLAYER))   # [82CF] 8C72
                else:
                    writes, sfx = _loop1_hit_outcome(rb, rw, di)
                    apply(writes)
                    for s in sfx:
                        emit_sfx(s)
                    return True
        di = (di + 0x12) & 0xFFFF                          # [83CE]
    return False


# --- loop2: player-vs-entity pickups (83D7..8617) — the ~23 effect handlers (names per cyxx level.c) -------
# Offsets confirmed from the ASM handler bodies (cross-checked vs cyxx level_update_player_collision):
ENTITY2 = 0x50A8           # the 52-entry pickup/entity list (objects 23+i in cyxx)
ENERGY = 0x27D6           # player energy (0..3)  [NOT lives — my earlier mislabel]
BONUS_ENERGY_CTR = 0x6BC9  # small-energy-bonus accumulator (6 -> +1 energy)
LETTERS_MASK = 0x6CA7     # BONUS letters bitmask
UTENSILS_MASK = 0x6CA8    # utensils/tools bitmask
CLUB_TYPE = 0x7B18        # equipped club/weapon type (0..3)
ITEM_COUNT_TBL = 0x6C12   # per-item collected count table
ITEM_TOTAL = 0x6C9E       # total collected items
SCORE_SPR_LUT = 0xA375    # [(num-57) -> score index]  (= (-0x5C8B)&0xFFFF)
FLYING = 0x6BC5           # flying power-up flag
CUR_ANIM = 0x4F27         # player current-anim
LIGHT_STATE = 0x6C04      # 0=on,1=off
LEVEL = 0x2D8A            # level number
LEVEL_DONE = 0x6BE6       # 1=level complete, 0xFF=game complete
SHAKE = 0x6BEA           # screen-shake counter
A33A = 0xA33A            # scratch: last consumed spr_num


class Loop2NeedsHelper(Exception):
    """A loop2 handler path needs an as-yet-unrecovered sub-routine (8D1B bones / 94F3 bomb / 65D6 life)."""


def _consume_link(rw, si):                                  # [853F] level_clear_item: consume [si+9] entity
    bx = rw((si + 9) & 0xFFFF)
    return {} if bx == 0xFFFF else {(bx + 4) & 0xFFFF: (0xFFFF, 2)}


def _count_and_score(rb, rw, si, num):
    """[85B6] shared food/collectible tail: bump the item count + add the lut score (spawned at 0x4A+lut)."""
    out = {}
    idx = (num - 0x39) & 0xFFFF                             # num-57
    out[(ITEM_COUNT_TBL + idx) & 0xFFFF] = ((rb((ITEM_COUNT_TBL + idx) & 0xFFFF) + 1) & 0xFF, 1)
    out[ITEM_TOTAL] = ((rw(ITEM_TOTAL) + 1) & 0xFFFF, 2)
    eff = (rb((SCORE_SPR_LUT + idx) & 0xFFFF) + 0x4A) & 0xFFFF
    if rw((si + 9) & 0xFFFF) != 0xFFFF:                     # [85CC] linked -> bump [0x2A7A]
        out[0x2A7A] = ((rw(0x2A7A) + 1) & 0xFFFF, 2)
    out.update(spawn_pickup_effect(rb, rw, eff, si))        # [860B] spawn at eff id
    return out


def loop2_handler(num, rb, rw, si, find_free):
    """Dispatch a pickup hit (ax=num=(spr_num&0x1FFF)-0x35) to its effect, in the ASM's chain order. Returns
    (writes, sfx). Raises Loop2NeedsHelper for the 3 paths whose bones/bomb/life sub-routines aren't recovered
    yet. (Names per cyxx level.c.)"""
    if num == 0x91:                                        # id 0xc6 [885F] "tap": clear fly timers, then count
        out = {}
        for k in range(0x14):                              # [8861] table 0x6EA9, 0x14 * 8
            out[(0x6EA9 + k * 8 + 7) & 0xFFFF] = (7, 1)
        out.update(_count_and_score(rb, rw, si, num))
        return out, [8]
    if num == 0xE2:                                       # id 0x117 [882A] end-of-level (level transition)
        lvl = rb(LEVEL); out = {}
        nxt = {2: 0xC, 0xD: 2, 6: 0xE, 0xF: 6}.get(lvl)
        if nxt is not None:
            out[LEVEL] = (nxt, 1)
        out[LEVEL_DONE] = (1, 1)
        return out, []
    if num == 0x102:                                      # id 0x137 [8859] game complete
        return {LEVEL_DONE: (0xFF, 1)}, []
    if num == 0xE4:                                       # id 0x119 [87FD] checkpoint
        out = {0x6BAD: (rw(PLAYER), 2), 0x6BAF: (rw(PLAYER_Y), 2)}
        for k in range(0x46):                             # [8809] reveal item 0x118 in the 0x8F1D table
            o = (0x8F1D + k * 7 + 4) & 0xFFFF
            if rw(o) == 0x118:
                out[o] = (0x119, 2)
        bx = rw((si + 9) & 0xFFFF)
        if bx != 0xFFFF:
            out[(bx + 4) & 0xFFFF] = ((rw((bx + 4) & 0xFFFF) - 1) & 0xFFFF, 2)
        return out, []
    if num == 0xAE:                                       # id 0xe3 [87E6] extra life
        raise Loop2NeedsHelper("extra-life 65D6")
    if num in (0xD, 0xB6, 0x2C, 0xE0):                    # ids 0x42/0xeb/0x61/0x115 [87AE..] club/weapon 0-3
        ct = {0xD: 0, 0xB6: 1, 0x2C: 2, 0xE0: 3}[num]
        w = {CLUB_TYPE: (ct, 1)}; w.update(_consume_link(rw, si))
        return w, [8]
    if num <= 0x14:                                       # ids 0x35-0x49 [85DA] small energy bonus
        out = dict(_consume_link(rw, si))
        ctr = (rb(BONUS_ENERGY_CTR) + 1) & 0xFF
        out[BONUS_ENERGY_CTR] = (ctr, 1)
        if ctr >= 6 and rb(ENERGY) != 3:
            out[ENERGY] = ((rb(ENERGY) + 1) & 0xFF, 1)
            out[BONUS_ENERGY_CTR] = (0, 1)
            out.update(spawn_pickup_effect(rb, rw, 0xE2, si))
        return out, [8]
    if num <= 0x2C:                                       # ids 0x4a-0x60 [8524] BONUS letters
        out = {}
        idx = (num - 0x27) & 0xFFFF
        if 0 <= idx <= 4:
            out[LETTERS_MASK] = (rb(LETTERS_MASK) | (1 << idx), 1)
        out.update(_consume_link(rw, si))
        return out, [8]
    if num <= 0x32:                                       # ids 0x62-0x67 [854F] utensils/tools
        out = {}
        idx = (num - 0x2D) & 0xFF
        out[UTENSILS_MASK] = (rb(UTENSILS_MASK) | (1 << idx), 1)
        if idx == 1:                                      # lighter -> reveal the 0x116 semaphore item
            for k in range(0x46):
                o = (0x8F1D + k * 7 + 4) & 0xFFFF
                if rw(o) == 0x116:
                    out[o] = (0x117, 2)
        out.update(_consume_link(rw, si))
        return out, [8]
    if num <= 0x40:                                       # ids 0x68-0x75 [8582] food (bounce or score)
        ydir = _s16(rw((si + 0xE) & 0xFFFF))
        if ydir < 0x80:                                   # low -> count + score (shared 85B6)
            return _count_and_score(rb, rw, si, num), [4]
        out = {(si + 0xE) & 0xFFFF: ((-ydir) & 0xFFFF, 2)}   # bounce up
        a, b, c, d, ret = rng_lcg(rb(0x2CEC), rb(0x2CED), rb(0x2CEE), rw(0x2CEF))
        out[0x2CEC] = (a, 1); out[0x2CED] = (b, 1); out[0x2CEE] = (c, 1); out[0x2CEF] = (d, 2)
        xv = 0x20
        if ret & 1:
            xv = (-0x20) & 0xFFFF
            out[SHAKE] = (7, 1)
        out[(si + 6) & 0xFFFF] = (xv, 2)
        out[(si + 4) & 0xFFFF] = (rw(A33A), 2)
        return out, [4]
    if num <= 0x4A:                                       # ids 0x76-0x7f [850A] flying power-up
        out = {}
        if rb(FLYING) == 0:
            out[FLYING] = (1, 1)
            out[CUR_ANIM] = (0xFF, 1)
            out.update(_consume_link(rw, si))
        return out, [8]
    if num <= 0xA6:                                       # ids 0x80-0xdb [85B0] collectibles -> score
        return _count_and_score(rb, rw, si, num), [8]
    if num == 0xAD:                                       # id 0xe2 [84F6] energy refill (+1 if < 3)
        out = {}
        if rb(ENERGY) < 3:
            out[ENERGY] = ((rb(ENERGY) + 1) & 0xFF, 1)
            out.update(_consume_link(rw, si))
            return out, [4]
        return out, []                                    # full -> nothing (the 8509 ret)
    if num in (0xA7, 0xA8):                               # ids 0xdc/0xdd [864F] trap hit (bones)
        raise Loop2NeedsHelper("trap-hit 867E/8D1B")
    if num == 0xA9:                                       # id 0xde [86B7] kill all monsters
        raise Loop2NeedsHelper("kill-all needs the 12-slot death_handler walk")
    if num == 0xAA:                                       # id 0xdf [870A] bomb
        raise Loop2NeedsHelper("bomb 94F3")
    if num == 0xB5:                                       # id 0xea [876C] light OFF
        out = {}
        if rb(LIGHT_STATE) != 1:
            out = {0x6C02: (0, 1), 0x6C01: (1, 1), 0x6C03: (0, 1), LIGHT_STATE: (1, 1)}
            out.update(_consume_link(rw, si)); return out, [1]
        out.update(_consume_link(rw, si)); return out, []
    if num == 0xB4:                                       # id 0xe9 [8790] light ON
        out = {}
        if rb(LIGHT_STATE) != 0:
            out = {0x6C01: (0, 1), 0x6C02: (1, 1), 0x6C03: (0, 1), LIGHT_STATE: (0, 1)}
        out.update(_consume_link(rw, si)); return out, []
    raise Loop2NeedsHelper(f"unmapped num {num:#x}")


_EARLY_SKIP = (0xE5, 0x12C, 0x132, 0x134, 0x136)          # [840A] ids that pass through (no consume, no effect)


def loop2(rb, rw, apply, emit_sfx, find_free):
    """[asm 83D7..8617] walk the 52-entry pickup list (0x50A8) vs the player; on a hitbox overlap of a
    collectible (`[si+5]&0x20`) entity, consume it and dispatch its effect. Applies writes via ``apply``;
    plays sounds via ``emit_sfx``. Raises Loop2NeedsHelper if a not-yet-recovered effect path is hit."""
    si = ENTITY2
    for _ in range(0x34):                                  # cx=0x34 (52)
        sid = rw((si + 4) & 0xFFFF)
        if (sid != 0xFFFF and _s16(rw((si + 0xC) & 0xFFFF)) <= 0xBC   # [83E0/83E9] live + not-yet-active
                and (rb((si + 5) & 0xFFFF) & 0x20)):                 # [83F3] collectible flag
            hit, hb = hitbox_overlap(rb, rw, si, PLAYER)             # [83FC] 8D7B (si=entity, di=player)
            apply(hb)
            if hit:
                aid = sid & 0x1FFF                                   # [8404/8407]
                if aid not in _EARLY_SKIP:
                    # [8426] consume: [0xA33A] stores the FULL spr_num (the &0x1FFF mask is applied AFTER,
                    # only for the dispatch), so the 0x2000 collectible flag stays in its high byte.
                    apply({(si + 4) & 0xFFFF: (0xFFFF, 2), A33A: (sid, 2)})
                    if aid in (0x1CA, 0x1CB):                        # [8432] boss projectile -> hit
                        raise Loop2NeedsHelper("boss-proj 8618/867E")
                    writes, sfx = loop2_handler((aid - 0x35) & 0xFFFF, rb, rw, si, find_free)
                    apply(writes)
                    for s in sfx:
                        emit_sfx(s)
        si = (si + 0x12) & 0xFFFF                                    # [860E]


def player_interaction_tick(rb, rw, apply, emit_sfx, find_free):
    """[asm 8295..8617] the whole player<->world interaction subsystem: loop1 (player-vs-enemy) then, unless
    loop1 took an early return, loop2 (player-vs-pickup)."""
    if loop1(rb, rw, apply, emit_sfx):
        return
    loop2(rb, rw, apply, emit_sfx, find_free)
