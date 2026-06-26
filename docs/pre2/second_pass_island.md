# Second per-frame pass — the secondary-entity / player-injection island (`1030:6913..698B`)

The frame loop runs the object-update walker (`684E..6913`, fully recovered → `object_tick`) and then **falls
through** into a SECOND pass at `6913`. This pass walks a separate, variable-stride entity list and projects
each on-screen entity into a free slot of the **main object list `0x4FD0`** so the moving-sprite renderer
(`26FA`) draws it. The list holds the **player** (entry 0) plus special entities (score popups, projectiles,
…).

## The pass loop (`6913..698B`)
- `si = 0x8489`; per entry (stride = `[si]`, advance `si += (signed)[si]`):
  - `[si] >= 0x32` → **end of list** (ret).
  - skip the entry if `[si+2] == 0xFFFF` (empty) **or** `[si+4] & 4` **or** (`[0xB198] != 1` **and** `[si+1] & 0x80`).
  - dispatch: `bl = [si+1]; bx = (bl << 1) & 0xFF` (8-bit, like the walker — the `0x80` bit is the skip flag
    above, masked off here); `call cs:[bx + 0x6AC3]`.
  - on **CF=0** (the entity was projected): an **animation-frame table lookup** (`6954..6981`) — scan the
    `0xA86F` table for a `0x7D01` marker matching the entity type `[si+1]&0x7F`, then walk to the frame
    matching `[si+2]-0x138`, and store that table pointer into the projected record's `[+0xC]`.

## Entry struct (variable size; first byte = stride)
`[+0]` stride · `[+1]` handler idx (+`0x80` skip flag) · `[+2]` sprite id (word) · `[+4]` mode byte ·
`[+5]` aux/flip · `[+9]` world X (word) · `[+0xB]` world Y (word) · (type-specific tail). Example player
entry (`0x8489`, idx 10): `0e 0a 72 01 00 14 05 ff 05 09 2d 1d 0a …`.

## Handler table `cs:[0x6AC3]` (contiguous right after the walker's `cs:[0x6AA9]` idx0-12)
| idx | addr | role |
|---|---|---|
| 0 | `7F6C` | (complex) |
| 1 | `7F26` | **`project_entity` — the shared worker (RECOVERED)** |
| 2 | `7EE2` | (complex) |
| 3 | `7ED8` | wrapper: project, then `[+4]=0x37` |
| 4 | `7EBF` | (complex) |
| 5–8 | `7EB5` | wrapper: project, then `[+4]=5` |
| 9 | `7E97` | wrapper: `[+0x11]=0`; project; `[+4]=[+4]|5` (`|0x80` if `[0x2D8A]==6`) |
| 10 | `7D9B` | **the PLAYER FSM** (≈0xFC bytes — the big remaining prize) |
| 11 | `7D6E` | wrapper: saturating timer `[+7]`/`[+6]`; project, `[+4]=0x37`, rng-jitter `record[+2]` |
| 12 | `7D1B` | (complex) |

## Recovered so far
- **`project_entity` (1030:7F26). VERIFIED** (`pre2/recovered/object_inject.py`, `tests/test_object_inject.py`;
  snapshot shadow 480/480 on 154531). On-screen cull (`on_screen_tile`/8022, already recovered) → allocate a
  free object slot (`find_free_object_slot`/806C) → copy X `[+9]`, Y `[+0xB]`, sprite `[+2]`, back-ptr into the
  record, zero velocity/state, flip byte from `[+5]`, set the entity mode `[+4]=0x17`. CF=1 (off-screen / no
  slot) leaves everything untouched.
- **`find_free_object_slot` (1030:806C). VERIFIED** — first `0x4FD0` slot with `[+4]==0xFFFF`.

## Remaining (next, in order)
1. The thin wrappers (idx3, idx5–8, idx9, idx11) — each is `project_entity` + a mode-byte write (idx11 also a
   timer + an rng jitter of the projected Y). Quick once shadowed.
2. The anim-frame table lookup (`6954..6981`, table `0xA86F`) — sets `record[+0xC]`.
3. The complex handlers idx0 `7F6C` / idx2 `7EE2` / idx4 `7EBF` / idx12 `7D1B`.
4. **The player FSM `7D9B`** — the heart of input→player-state; the main prize (enables a self-driving
   input→logic→state→render loop). Likely needs input wiring + its own sub-island.
5. Compose `second_pass_tick` over the list, shadow-verify whole-pass, then fold into the live `object_tick`
   collapse (it already resumes at `6913`, so the same hook can run both passes).
