# Combat / pickup interaction island (1030:88D7)

Status: **MAPPED (Stage 0)**, first leaves recovered. This is the per-frame pass that resolves the player and
the player's thrown weapons against enemies (damage/kill) and against bonus tiles (pickup/score). It is a real
gameplay-logic island, comparable in size to the object-update walker — recover it **leaf-first, shadow-verify,
then compose** (the object_tick precedent). Recovered code lands in `pre2/recovered/combat_interaction.py`.

## Boundary

| addr | role |
|------|------|
| `88D7` | orchestrator. `[0xA312]=1`; for the 4 projectile slots `0x4F2E` (stride 0x12): if `[si+4]!=-1` → `8C21`; if it did **not** hit an enemy (CF=0) → `899E`. Then unless `[0x6BC5]` (scripted pose): the player sprite `0x4F0A` → `8C21`/`899E`, with a special `[0x4F2A]` (player Yvel) bounce on a miss. `[0xA312]=0`; ret. |
| `8C21` | **source-vs-ENEMY collision/damage.** Scan the 12 object slots `0x4FD0` (stride 0x12). Skip empty (`[di+4]==-1`), dead (`[di+0xE]==0xFF`), or non-collidable (`[bx+4]&0x10`, bx=`[di+6]` def-ptr). `8D7B` proximity; on hit: `[di+5]|=0x40`, `[di+0xF] -= [0x7B19]` (HP). If HP underflows (kill): `dx=2; call 0x282` (play_sfx) + `8C72` (death). Else knockback `[di] -= [di+8]>>2`. Consume source `[si+4]=0xFFFF`; **return CF=1**. — **RECOVERED** (`projectile_vs_enemies`; shadow 170 calls / 5 demos, 0 mismatch, incl. 4 kills→death_handler) |
| `899E` | **source-vs-BONUS pickup.** Scan the 80-entry bonus-cell list `0x8C8D` (stride 5, `[+3]`=packed x/y map offset). Coarse gate `|Δx_cell|<=1` and a `0x10` y window vs `bp=[si+2]-0x10`. On a candidate: `[si+4]=0xFFFF`, call `8A5A`; if it collects (CF=1), **flood-fill** all connected cells (8-adjacency, deduped via `0xA2A8`) calling `8B6E` per cell. Returns CF=collect. — **RECOVERED** (`bonus_pickup_scan` + `_flood_collect`; shadow 150 calls / 4 demos, 0 mismatch, incl. 6 collects; the flood-fill *inner* 8B6E-entry collect is unwitnessed but disasm-faithful) |

## Sub-routines (leaves)

| addr | role | status |
|------|------|--------|
| `8BF6` | pack-spawn-pos: `[di+3]` cell coords → `[0xA336]=x<<4`,`[0xA338]=y<<4`; cx=1 | **VERIFIED** (`pack_spawn_pos`; shadow 1/1 demo 105310) |
| `8C13` | roll-bonus-sprite-id: rejection-sample `rng_lcg` → `0x2080 + (ret&0x7F)`, reroll if `>=0x5F` | **ASM_MATCHED** (`roll_bonus_sprite_id`; composes verified rng_lcg; unwitnessed) |
| `8D7B` | **enemy sprite-hitbox proximity test** (the keystone). Coarse `|Δx|<0x40 & |Δy|<0x46`, then a Y- then X-axis AABB overlap using per-class half-extent tables `[0x7190]`/`[0x7191]` (stride 2) + `[0x752A]` (stride 2), indexed by `(id & 0x1FFF)*2` (low byte kept!). `[0xA312]` selects the full (un-halved) tolerance; `[0x4F2A]`/non-player gate the vertical-detail write `[0xA330]`/`[0xA331]`; returns CF. | **VERIFIED** (`hitbox_overlap`; shadow 1895 calls / 6 demos, 0 mismatch — CF + detail) |
| `8D1B` | score/effect burst emitter: spawn `cx` sprites into free slots `0x50A8..0x52E8` (id `[0xA33A]`, pos `[0xA336]/[0xA338]`, alternating Xvel sign, stepping ax/dx by 0x10 on even spawns, ax zeroed past the 12th) | **ASM_MATCHED** (`spawn_effect_burst`; shadow 2 calls incl. a 6-spawn burst, 0 mismatch; `di>0xC` zero-branch unwitnessed) |
| `8875` | debris-element spawn into the `0x5450` pool (16 slots): `[+4]=sprite`,`[+0]/[+2]=pos`,`[+0xC]=0x2C`,`[0xA33E]=slot`; bump 32-bit score `[0x6C0E]` via `[(sprite-0x4A)*2-0x5CAD]`; free a back-ref effect slot if `si>=0x50A8` | **ASM_MATCHED** (`spawn_debris_element`; shadow 7 kills, 0 mismatch) |
| `80CB` | advance the dying enemy's anim-script pointer `[di+0xC]` past the next `0x7D00` death marker | **ASM_MATCHED** (`advance_death_anim`; shadow 3 kills, 0 mismatch) |
| `8C72` | **enemy-death handler**: loop `8875` ×count (count table `[bx-0x5C0F]` by `[di+0x10]>>3 & 7`, scattering pos `+=9/+7`, staggering `[elem+0xC]`), restore enemy pos, mark dead `[di+0xE]=0xFF`, then either (`[def+4]&1==0`) spawn 6 death-bonus sprites id 0x2046 via `8D1B`, or (`&1`) the `80CB` + knockback-launch path (`[di+0xA]` Yvel, `[di+8]` Xvel from `[0x7B19]` damage, signed by the **attacker's** facing `[src_si+5]`) | **ASM_MATCHED** (`death_handler`; shadow byte-exact BOTH paths — 3 launch kills (115215) + 1 bonus kill (140619), 0 mismatch; overlay compose) |
| `8B6E` | bonus collect tail (the `8B77` body, reached by jmp from `8A5A`): `inc [0x2A76]`; clear the cell `[di+3]=0xFFFF`; restore tile id `[di+1]` into the level map (es=`[0x2DDA]`) at the old `[di+3]` offset; on-screen → redraw-dirty `[0x6BBD]/[0x2DF4]/[0x2DE0]=0x55AA` + the `453B`+`3B77` tile blit (render side-effect) | **ASM_MATCHED** (`bonus_collect_tail`, state contract; shadow on the 1 witnessed collect in 105310, 0 mismatch; the blit is the live hook's render step) |
| `8A5A` | **bonus hit handler**: spawn a pickup sparkle (`5E41`) at the cell; then a counter (bit7) path (dec `[cell+2]`; underflow→collect, else random-id sparkle + collect) or the normal path with a frame debounce (`[0xA33C]` vs `[0x6BD5]`, <6 → CF=0), level-dependent (`[0x2D8A]`) score-popup bursts (`8D1B`) keyed on `[cell+2]&0x40`, then decrement+collect (`8B6E`) on underflow. Returns CF = real collect. | **ASM_MATCHED** (`bonus_hit_handler`; overlay compose of 5E41+8BF6+rng+8C13+8D1B+collect; shadow 81 calls / 3 demos — bit7+collect+rng (105310) + debounce + `!=0x40` burst (190542), 0 mismatch; the `==0x40` two-burst + bit6==0 random paths are unwitnessed but disasm-faithful) |
| `5E41` | spawn a pickup-sparkle (sprite 0x35) at (ax,dx) into the `[0x4F76..0x4FBE]` effect ring; advance `[0x6BBE]` down 0x12 with wrap | **ASM_MATCHED** (`spawn_pickup_sparkle`; shadow 80 calls / 2 demos, 0 mismatch) |

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
2. ✅ `8D1B` (`spawn_effect_burst`) + `8875` (`spawn_debris_element`) + `80CB` (`advance_death_anim`) +
   `8C72` (`death_handler`, both paths) — ALL DONE, shadow byte-exact.
3. ✅ `8C21` (enemy damage, `projectile_vs_enemies`) — DONE, shadow byte-exact (both kill + knockback paths).
   The whole projectile/player-vs-ENEMY side of the island is now recovered.
4. ✅ `899E` (source-vs-bonus pickup, `bonus_pickup_scan`) — DONE, shadow byte-exact. The whole bonus side
   (`8A5A` + `5E41` + `8B6E` + the flood-fill) is recovered.
5. **Next: live-hook `88D7`** — the orchestrator (`8C21` then `899E` per projectile + the player). Both
   `projectile_vs_enemies` and `bonus_pickup_scan` are recovered; the live hook applies their write contracts,
   emits the SFX, performs the on-screen tile re-blit for collects (the render side-effect), and the player
   `[0x4F2A]` bounce on a miss. Then verify-mode coverage.

Gated flags to respect: `[0x6BC5]` (scripted pose — skips the player pass), `[0xA312]` (set across the pass;
read by `8D7B` to relax the player-vs-enemy bounce test).
