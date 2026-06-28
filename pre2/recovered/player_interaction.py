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
