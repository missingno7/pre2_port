# Combat / pickup interaction island (1030:88D7)

Status: **MAPPED (Stage 0)**, first leaves recovered. This is the per-frame pass that resolves the player and
the player's thrown weapons against enemies (damage/kill) and against bonus tiles (pickup/score). It is a real
gameplay-logic island, comparable in size to the object-update walker — recover it **leaf-first, shadow-verify,
then compose** (the object_tick precedent). Recovered code lands in `pre2/recovered/combat_interaction.py`.

## Boundary

| addr | role |
|------|------|
| `88D7` | orchestrator. `[0xA312]=1`; for the 4 projectile slots `0x4F2E` (stride 0x12): if `[si+4]!=-1` → `8C21`; if it did **not** hit an enemy (CF=0) → `899E`. Then unless `[0x6BC5]` (scripted pose): the player sprite `0x4F0A` → `8C21`/`899E`, with a special `[0x4F2A]` (player Yvel) bounce on a miss. `[0xA312]=0`; ret. |
| `8C21` | **source-vs-ENEMY collision/damage.** Scan the 12 object slots `0x4FD0` (stride 0x12). Skip empty (`[di+4]==-1`), dead (`[di+0xE]==0xFF`), or non-collidable (`[bx+4]&0x10`, bx=`[di+6]` def-ptr). `8D7B` proximity; on hit: `[di+5]|=0x40`, `[di+0xF] -= [0x7B19]` (HP). If HP underflows (kill): `dx=2; call 0x282` (play_sfx) + `8C72` (death debris). Else knockback `[di] -= [di+8]>>2`. Consume source `[si+4]=0xFFFF`; **return CF=1**. |
| `899E` | **source-vs-BONUS pickup.** Scan the 80-entry bonus-cell list `0x8C8D` (stride 5: `[+3]`=x cell, `[+4]`=y cell). Coarse gate `|Δx|<=1` and a `0x10` y window vs `bp=[si+2]-0x10`. On a candidate: `[si+4]=0xFFFF`, `8A5A` (the hit handler → `5E41`); on confirm, walk a secondary on-screen list and `8B6E` per breakable cell. Picks a score-popup id into `[0xA33A]` (branching on `[0x2D8A]` level id), bursts effects via `8D1B`, accumulates `[0xA336]/0xA338/0xA33C`. |

## Sub-routines (leaves)

| addr | role | status |
|------|------|--------|
| `8BF6` | pack-spawn-pos: `[di+3]` cell coords → `[0xA336]=x<<4`,`[0xA338]=y<<4`; cx=1 | **VERIFIED** (`pack_spawn_pos`; shadow 1/1 demo 105310) |
| `8C13` | roll-bonus-sprite-id: rejection-sample `rng_lcg` → `0x2080 + (ret&0x7F)`, reroll if `>=0x5F` | **ASM_MATCHED** (`roll_bonus_sprite_id`; composes verified rng_lcg; unwitnessed) |
| `8D7B` | **enemy sprite-hitbox proximity test** (the keystone). Coarse `|Δx|<0x40 & |Δy|<0x46`, then a Y- then X-axis AABB overlap using per-class half-extent tables `[0x7190]`/`[0x7191]` (stride 2) + `[0x752A]` (stride 2), indexed by `(id & 0x1FFF)*2` (low byte kept!). `[0xA312]` selects the full (un-halved) tolerance; `[0x4F2A]`/non-player gate the vertical-detail write `[0xA330]`/`[0xA331]`; returns CF. | **VERIFIED** (`hitbox_overlap`; shadow 1895 calls / 6 demos, 0 mismatch — CF + detail) |
| `8C72` | death-debris spawner: id `[bx+8]+0x4A`, count from table `[bx-0x5C0F]` indexed by `[di+0x10]>>3 & 7`; loop calling `8875` scattering sprites (`+=9/+7` per step) | TODO |
| `8D1B` | score/effect burst emitter: spawn `cx` sprites into free slots `0x50A8..0x52E8` (id `[0xA33A]`, pos `[0xA336]/[0xA338]`, alternating Xvel sign, stepping ax/dx by 0x10) | TODO |
| `8B6E` | breakable-tile rewrite + redraw: `inc [0x2A76]`; write the tile map (es=`[0x2DDA]`); on-screen → `453B` + `3B77` blit; set dirty `[0x2DF4]/[0x2DE0]=0x55AA` | TODO (**unwitnessed** in current demos) |
| `8C13`/`8C72` helper `8875` | (debris element spawn) | TODO |
| `8A5A` | bonus hit handler (→ `5E41`) | TODO |

## Witnesses (census across demos, scratchpad census_88d7.py)

- Enemy collision `8C21` fires in most gameplay demos; the **kill path** (`8C72`) is witnessed in
  `demo_pre2_20260626_115215` (3 kills) and `demo_pre2_20260626_140619` (1).
- Bonus interaction `899E`/`8A5A` is witnessed in `demo_pre2_20260626_105310` (5 candidates, 1 confirmed hit
  → the single `8BF6`) and `demo_pre2_20260627_120536`.
- `8B6E` (breakable-tile rewrite) and `8C13` (the `[di+2]&0x40==0` bonus branch) are **not witnessed** by the
  current demos — recover from disasm, then verify when a witness exists (e.g. breakable-tile / snow levels).

## Recovery plan

1. ✅ **`8D7B` (keystone)** — DONE, VERIFIED (`hitbox_overlap`, 1895 shadow calls 0 mismatch). The index keeps
   the id low byte: `(id & 0x1FFF) << 1`.
2. `8C72` + `8875` (death debris) and `8D1B` (score burst) — both witnessed via the kill/pickup demos.
3. Compose `8C21` (enemy damage) — shadow its net contract (enemy HP/flag/knockback + projectile consume +
   sfx/debris) over the kill demos; then `899E` (bonus pickup); then the `88D7` orchestrator + live hook.

Gated flags to respect: `[0x6BC5]` (scripted pose — skips the player pass), `[0xA312]` (set across the pass;
read by `8D7B` to relax the player-vs-enemy bounce test).
