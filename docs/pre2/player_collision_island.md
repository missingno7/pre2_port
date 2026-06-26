# Player collision / tile-interaction island (`1030:5A96`)

The player ground/tile collision, called from the player update at `5A41` (after the Y integrate). Fires once
per player frame (~2006×/L1). This is a bounded sub-island with a small tile-type handler table — the last big
ASM piece of `player_update` and the prerequisite for the full `player_update` live collapse.

Status: **boundary mapped (OBSERVED)** — recovery not started. Heavily witnessed (so cleanly verifiable).

## Boundary + structure
- **`5A96..5B80` main collision** (`ret` at `5B80`). Computes the player's tile cell from Y/X, reads the tile,
  range-checks vs the camera (`[0x2DE4]`/`[0x2DE6]`), calls the tile-interaction worker `5B81`, then a vertical
  tile-scan loop (`5B6F-5B7B` calling `5C92`/`5CAC`, ~3584×) and the fall-off-edge path `63B5` (~820×).
- **`5B81` tile-interaction worker**: reads the tiles around the player from the map (`es:[di]`, `es=[0x2DDA]`),
  remaps tile ids via the table `0x7F5E`, manages the "tile being eaten/destroyed" state (`[0x6BAB]`), and
  dispatches the **tile-type handler `call word ptr [bx+0x7D9B]`** at `5C04` (DS-relative table).

## Tile-type handler table `cs:[0x7D9B]` (bx = tile_type*2) — witnessed
| bx | handler | fires (L1) | role |
|---|---|---|---|
| 0x00 | `0x65EF` | 811 | type 0 — snap-down-if-solid-below else fall (slope-aware) |
| 0x02 | `0x6641` | 1225 | type 1 — land (`call 0x641F`) |
| 0x04 | `0x6657` | 46 | type 2 — land + slope shift `[0x4F24]=1` |

(`0x6660`/`0x6669` are the `[0x4F24]=2`/`=3` slope variants — not yet witnessed.) The handlers are thin
wrappers over two shared core routines + the slope helper.

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
2. **`0x641F` land** — recover next (needs the map-read bridge `es=[0x2DDA]`/`di` + the `5E18` landing-dust
   register state; the soft-land path is heavily witnessed on flat ground, the hard-land on jump+land frames).
2. Recover `0x641F` land (reads the tile-property table `0x8E1D` + slopes) — the core ground response.
3. Recover the 3 tile handlers (`65EF/6641/6657`) as thin compositions of the above.
4. Recover `5B81` tile-interaction (map reads + the `0x6BAB` eat-state + the handler dispatch).
5. Recover the `5A96` main body (tile-cell calc + camera range-check + the `5CAC` scan loop + `63B5`).
6. Compose `collision(mem)` and shadow-verify the full write-contract byte-exact; then it unblocks the
   `player_update` live collapse.

The bridge needs the map segment `es=[0x2DDA]` + `di` (player tile pointer) and the camera `[0x2DE4]/[0x2DE6]`.
