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
