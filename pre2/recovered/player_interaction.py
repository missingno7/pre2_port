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
