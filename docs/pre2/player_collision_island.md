# Player collision / tile-interaction island (`1030:5A96`)

The player ground/tile collision, called from the player update at `5A41` (after the Y integrate). Fires once
per player frame (~2006×/L1). This is a bounded sub-island with a small tile-type handler table — the last big
ASM piece of `player_update` and the prerequisite for the full `player_update` live collapse.

Status: **✅ FULLY RECOVERED + LIVE** (`collision(rb, rw, read_es)` in `pre2/recovered/player_collision.py`,
hooked at `5A96` by `pre2/checkpoints/player_collision.py`). The whole `5A96` routine reproduces the ASM
write-contract byte-for-byte. Live-hybrid mode runs the native routine (apply `(ds_writes, map_writes)` + emulate
the RET); verify mode diffs every predicted byte vs the ASM oracle at the `5B80` ret — **clean across all 28
gameplay demos, 0 divergences** (`--verify-hooks` sweep). The off-camera trigger (`65B3`, ground idx6 + `5B26`)
and the ceiling solid
side-nudge (`5C44`) — initially fail-loud — were surfaced by the live verify trajectory and recovered. This was
the largest remaining ASM chunk of `player_update`.

## Boundary + structure
- **`5A96..5B80` main collision** (`ret` at `5B80`). Computes the player's tile cell from Y/X, reads the tile,
  range-checks vs the camera (`[0x2DE4]`/`[0x2DE6]`), calls the tile-interaction worker `5B81`, then a vertical
  tile-scan loop (`5B6F-5B7B` calling `5C92`/`5CAC`, ~3584×) and the fall-off-edge path `63B5` (~820×).
- **`5B81` tile-interaction worker** (mapped; witnessed enter 2082×, dispatch 2082×, ceiling 1691×, bridge-dip
  3-5×, off-top `63B5` 0×). Three parts:
  1. **Ground dispatch**: `di = bx+0x100` (tile below the player); tile id `es:[di]` remapped via `0x7F5E` →
     `bx = type*2`; dispatch the **ground tile handler `call [bx+0x7D9B]`** (`5C04`) — the land/fall/slope core.
  2. **Bridge-dip** (`5BB8`): the platform-sag-under-weight effect — a bridge/platform tile (prop bit `0x20`,
     `[bx+0x805E]`) visually **dips down under the player's weight** and springs back when stepped off. NOT
     eating, NOT a breakable door. `[0x6BAB]` tracks the one tile currently dipping; on contact it advances that
     tile's graphic (`es:[di]=id+1` — the sag frames) and dirties the grid (`5C7B`: `[0x2DF4]=1`/`[0x2DE0]=
     0x55AA` or `653D` draw); stepping onto a new bridge tile first runs the previous one back up (the
     `5BC4-5BE6` loop, advancing it until its `0x20`-flagged cycle completes). Witnessed in the rocky slope
     level (`001513`) as the grass/stone platform tiles (`0xDE/0xE0`) dipping as the caveman walks across.
  3. **Ceiling collision** (`5C16`, common, when not falling): `bx-0x100` (tile above); remap via `0x7E5E`/
     `0x805E`; dispatch a **second table `call [bx+0x7DA9]`** (ceiling-tile handler) which returns "solid" in
     `ah&1`; if rising into a solid ceiling, nudge X by ±2 to slip past an open side (`es:[di±dx+0x100]`).
  Above-the-top (`Y<=-1`): `63B5` + `[0x6BF3]=0xFF` (never witnessed).

## Three handler tables (DS-relative, bx = type*2)
- `cs:[0x7D9B]` — **ground** tile handlers (land/fall/slope), dispatched at `5C04` (remap `0x7F5E`).
- `cs:[0x7DA9]` — **ceiling** tile handlers, dispatched at `5C33`; returns "solid" in `ah&1` (remap `0x805E`).
- `cs:[0x7D95]` — **side/body** handlers (horizontal wall collision), dispatched at `5CA4`/`5CC6` from the
  vertical scan loop (remap `0x7E5E`). Note `0x7D95 = 0x7D9B - 6`, so idx≥3 alias the ground handlers.

### Side table `cs:[0x7D95]` (`collision_side_handler`, ✅ VERIFIED 3208 byte-exact)
| idx | handler | role | confidence |
|---|---|---|---|
| 0 | `0x652C` | if tile side-solid (`0x805E[tile]&0x10`) push a wall-impact marker `(X<<3,Y<<3)` into `0x6EA9` (`64FA`); else no-op | VERIFIED (no-op path); ASM_MATCHED (the `64FA` marker push never fires) |
| 1 | `0x6539` | wall block = `collision_hblock` (undo X step, stop) | VERIFIED (22+83+1+4) |
| 2 | `0x65AF` | special level trigger | fail loud |
| 3-8 | ground `65EF`… | alias the ground handlers | unwitnessed in side scan |

The scan loop walks `dh/0x10` cells up from the foot cell (`dh` = anim height from `0x7191`); `5C92` dispatches
the first cell unconditionally, `5CAC` the rest only for remapped tile types 2/4. The wall-marker list `0x6EA9`
is 10×8-byte records (slot free when leading word `0x55AA`); `64FA` writes `(X<<3, Y<<3)` + three zero bytes.

### Ceiling table `cs:[0x7DA9]` (`collision_ceiling`, ✅ VERIFIED 247+53+97 byte-exact)
| idx | handler | role |
|---|---|---|
| 0 | `0x6672` | `ret` — no-op (no ceiling tile) |
| 1 | `0x6673` | head-bump: if rising (Yvel≠0) zero Yvel + snap Y below the ceiling (`(&0xFFF0)+0x10`); if Yvel==0 the `668B` push-out-of-solid branch (unwitnessed → fail loud) |
| 2 | `0x65AF` | special level trigger (`[0x27D8]`/`[0x6BE4]`) — unwitnessed → fail loud |

The handler index is `0x805E[tile_above]&0xF`; tiles 5/6/7/8 → idx1, tile 0x26 → idx2, rest → idx0. The "solid"
flag for the side-nudge is `0x7E5E[player_tile]&1` (read pre-dispatch, saved across the call). The `5C44-5C72`
corner-slip (X±2) and the Yvel==0 push-out are both gated on `0x7E5E`-solid tiles, which are absent in all three
head-collision demos (`015602`/`015822`/`015934`, table all-zero) → unwitnessed, fail loud.

## Tile-type handler table `cs:[0x7D9B]` (`collision_ground_handler`, ✅ idx0/1 VERIFIED 3216 byte-exact)
| idx | handler | role | confidence |
|---|---|---|---|
| 0 | `0x65EF` | snap-down-if-reachable-tile-below else fall (slope-aware) | VERIFIED (190+532+761+16+76) |
| 1 | `0x6641` | plain land (`call 0x641F`) | VERIFIED (154+149+1236+85+17) |
| 2 | `0x6657` | land + slope shift `[0x4F24]=1` | ASM_MATCHED (unwitnessed) |
| 3 | `0x6660` | land + slope shift `[0x4F24]=2` | ASM_MATCHED (unwitnessed) |
| 4 | `0x6669` | land + slope shift `[0x4F24]=3` | ASM_MATCHED (unwitnessed) |
| 5 | `0x6645` | `[0x4F24]=0`; `[0x6BE1]!=0` ? fall : land | ASM_MATCHED (unwitnessed) |
| 6 | `0x65AF` | special level trigger | fail loud (unwitnessed) |
| 7 | `0x6672` | `ret` no-op | trivial |

idx 0 (`65EF`): at rest (Yvel==0), if a reachable solid/slope tile sits one row below (prop≠0 and slope offset
`< 0x10`), step the player down a row (`[0x4F1E]+=0x10`) and land on it; otherwise mark airborne. The handler
index is `0x7F5E[foot_tile]*2`; the foot tile is `es:[bx+0x100]` (player tile + one row). The handlers are thin
wrappers over the verified land/fall/slope cores — the byte-exact shadow (5C04→5C08) carries the landing-dust
ring writes too (the `5E18` ungated trail emit, full-word X/Y/id).

## Core routines
- **`0x641F` land-on-ground** (mapped, witnessed): `[0x4F24]=0`; if Yvel<0 (rising) → `0x6401` (airborne); else
  `[0x6BC7]=0`, snap `[0x4F1E]&=0xF0` (Y to tile top). Foot tile prop (`es:[di]`→`[tile+0x8E1D]`) nonzero ⇒ add
  `0x661A` slope offset capped by `sar(Yvel,4)`; else the below tile (`es:[di-0x100]`) nonzero ⇒ add `slope-0x10`.
  Then the **landing impact** (`647C`): if the fall counter `[0x6BD2] <= 4` → soft land `64D9` (`[0x4F2A]=0`,
  sat-dec `[0x6BE0]`, `[0x6BD1]=0`, `[0x6BF3]=2`, `[0x6BCA]=[0x4F1E]`); else emit landing dust (`5E18`), and if
  the drop `[0x4F1E]-[0x6BCA] >= 0x20` and `Yvel >= 0x50`: update `[0x6BCA]`, on a hard fall
  (`[0x6BD2]>=0x14` and `Yvel>0xA0`) set **camera shake `[0x6BEA]=8`**, and if `[0x6BD2]>0xA` bounce
  (`[0x4F2A]=-0x20` unless `[0x8166]&1`) + set the land anim frame (`[0x4F20]=(…&0xE000)|0xC`) + `[0x6BD2]=0`.
- **`0x6401` fall / no-ground**: `[0x6BF3]|=1` (set the airborne flag); `ret`.
- **`0x6407` horizontal block**: `[0x4F1C]-=sar(Xvel,4)`; `[0x4F22]=0` (undo the X step, stop) — wall hit.
- **`0x661A` slope height offset**: if `(prop&0x30)`: `quot=(X&0xF)//3`; `prop&0x10` ? `quot+(prop&0xF)` :
  `(prop&0xF)-quot`; sign-extend. Else return prop unchanged.
- **`0x6673`** (nearby): zero Yvel + snap Y up (`[0x4F2A]=0; [0x4F1E]=([0x4F1E]&0xFFF0)+0x10`).

## Tables
- `0x8E1D` — tile-property table (tile id → property byte: solid / slope flags `0x30` / slope dir `0x10` /
  height `0x0F`). Also read as `[bx-0x71E3]` (== `+0x8E1D`).
- `0x7F5E` — tile-id remap (used by `5B81` before the handler dispatch).
- `0x7191` — a tile lookup used by the main `5A96` body.

## Write-contract (what the whole collision mutates, witnessed)
`[0x4F2A]` Yvel (852×, zeroed/clamped on contact), `[0x4F1E]` Y (34×, snapped), `[0x6BD2]`/`[0x6BD1]` (482×),
`[0x6BF3]` airborne flag (92×), `[0x6BD0]` (29×), `[0x4F24]` slope shift (6×).

## Witness demos
- Flat (no slopes): `102854` (L1), `112253` (L6) — most ground is flat, so `0x661A`/slope paths are sparse here.
- **Sloped/slippery ("penguin") level: `20260626_001513`** (slope ×16) + `102854` has a few (×8) — use these to
  witness `0x661A` and `0x641F`'s slope branch.

## Recovery plan (next)
1. ✅ Leaves recovered+verified: `0x6401` fall (791+138), `0x6407` h-block (98), `0x661A` slope (16+8 on the
   slope demos).
2. ✅ **`0x641F` land recovered+verified** (`collision_land`): 1272/1272 (L1) + 149/149 (slope demo), byte-exact
   over all three exits (rising/soft/hard). The `5E18` landing dust = `player_emit_trail` ungated; the map read
   is `read_es(di)` → `[tile+0x8E1D]`.
3. ✅ **Ceiling collision recovered+verified** (`collision_ceiling`, `5C16`): 247+53+97 byte-exact on the
   head-collision demos. Witnessed = idx0 noop + idx1 head-bump (always rising); the Yvel==0 push-out, idx2
   trigger, and `0x7E5E`-solid side-nudge are unwitnessed and fail loud.
4. ✅ **Ground tile-handler dispatch recovered+verified** (`collision_ground_handler`, `5C04`): 3216 byte-exact
   across 5 demos for idx 0/1 (the only witnessed indices); idx 2-5 ASM-matched, idx 6 fail-loud, idx 7 noop.
5. ✅ **Bridge-dip recovered+verified** (`collision_bridge_dip`, `5BB8`): 3011 calls byte-exact (7 real dip/spring
   events on `001513`, tiles 0xDE-0xE1). Returns `(ds_writes, map_writes)`; writes the tile map (`es:[di]`±1 sag
   frames) + `[0x6BAB]` + grid-dirty flags (`5C7B`→`[0x2DF4]`/`[0x2DE0]`; the `653D` direct-redraw path is
   unwitnessed → fail loud).
6. ✅ **Side-scan dispatch recovered+verified** (`collision_side_handler`, `cs:[0x7D95]`): 3208 byte-exact.

### Remaining: the `5A96` main-body composition (the capstone → unblocks the `player_update` collapse)
All three handler tables + the bridge-dip + every leaf core are now recovered & byte-exact. The main body
(`5A96..5B80`) glues them:
1. **Cell calc** (`5A99-5ACB`): `row=(Y>>4)-1`, `col=X>>4`, cell `bx=col+(row<<8)`; `dh=0x7191[anim&0x1F00>>...]`
   (player height extent); X edge offset `9/-9/0` from `sign([0x4F22])`.
2. **Camera range check** (`5ACD-5B16`): if `|Y>>4-[0x2DE6]|>0xB` or `|X>>4-[0x2DE4]|>0x14` or off the vertical
   map bounds → out-of-range: `[0x2D8A]==0xE` ? `[0x6BE5]=0xFF` : `65AF` trigger.
3. `[0x6BF3]=0`; **call the `5B81` worker** (bridge + ground dispatch + ceiling — all done).
4. **Post-worker fall** (`5B31-5B54`): if the worker set airborne (`[0x6BF3]==1`): `[0x6BFE]==0` ? `64DF`
   (soft-land tail) : `63B5` (off-top fall anim) + if `Yvel>0` inc the fall counter `[0x6BD2]`; else `[0x6BD2]=0`.
5. **Side scan loop** (`5B5B-5B7B`): from the X-edge-offset foot cell, call `5C92`/`5CAC` (side dispatch, done)
   for `dh/0x10` rows upward.
Sub-leaves still to transcribe for the compose: `63B5` off-top fall-anim (calls `62B1`/`6309` dust spawners),
`64DF` soft-land tail (disasm'd: sat-dec `[0x6BE0]`, `[0x6BD1]=0`, `[0x6BF3]=2`, `[0x6BCA]=Y`). Then assemble
`collision(mem) -> (ds_writes, map_writes)` and shadow-verify the whole `5A96` write-contract at the `5B80` ret.
4. `5A96` main body (tile-cell calc + camera range-check + the `5CAC` scan loop) → compose `collision(mem)`
   and shadow-verify the full write-contract → unblocks the player_update live collapse.
2. Recover `0x641F` land (reads the tile-property table `0x8E1D` + slopes) — the core ground response.
3. Recover the 3 tile handlers (`65EF/6641/6657`) as thin compositions of the above.
4. Recover `5B81` tile-interaction (map reads + the `0x6BAB` bridge-dip + the handler dispatch).
5. Recover the `5A96` main body (tile-cell calc + camera range-check + the `5CAC` scan loop + `63B5`).
6. Compose `collision(mem)` and shadow-verify the full write-contract byte-exact; then it unblocks the
   `player_update` live collapse.

The bridge needs the map segment `es=[0x2DDA]` + `di` (player tile pointer) and the camera `[0x2DE4]/[0x2DE6]`.
