# Player FSM + input island (`1030:~5890..5A95`)

The per-gameplay-frame **player update** — input → FSM state → kinematics. Staged recovery (one leaf at a
time, shadow-before-live). The player is NOT in the object lists; it has its own struct + FSM.

## Boundary map (Stage 0)
- **Dispatcher `6822`** (called from the main loop @0220): `[0x91FE]!=0xFF → 70D7` (BOSS update, owns
  `[0x91FF]/[0x9201]`); `[0x2D8A]==5 → 6D34` (a boss/level-sequence orchestrator, table-driven waves — NOT the
  player); `[0x2D8A]==9 → 6ADD`; then falls through to the object walker (`684E` = `object_tick`).
- **Normal platforming is mode `[0x2D8A]==0`** — `6822` only runs the walker then; the **player update is a
  separate main-loop routine** (entry `~5890`, register-push prologue), not under `6822`.
- **Player update `~5890..5A95`** (fires ~once/frame in mode 0): read input flags → set facing `[0x4F25]` +
  the run/accel counter `[0x7B1A]` → `call cs:[bx+0x7D2F]` (the **per-state handler**, `5A0B`) → common
  kinematics: X integrate (`5A0F`), Y integrate (`5A36`), ground/tile collision (`5A96`), then a block of
  per-frame timer decrements (`[0x6BCD/0x6BCE/0x6BEA/...]`).
- **`7D9B`** (2nd-pass idx10) is NOT the player — it reads the player pos and projects a player-*following*
  trail/companion entity (offset by the cyclic table `[0xA341]`, ground-snapped). The PLAYER sprite render
  handoff is via the 2nd-pass projection path (`project_entity`, already live) — exact entry TBD.

## Input contract (Stage 1)
- **Keyboard ISR `182C`** reads port 0x60 → stores the raw scancode at `[0x2874]` (used by the MENU `99xx`,
  not gameplay).
- **6 player input flags `[0x27E8..0x27ED]`**, decoded at `~0x0E00` from the keyboard, consumed by the player
  FSM. Confirmed roles so far (from the FSM's use):
  - `[0x27EC]` → facing **right** (`[0x4F25]=+1`), `[0x27ED]` → facing **left** (`[0x4F25]=-1`) [asm 58BF-58FC]
  - `[0x27EA]` → run **accelerate** (`[0x7B1A]++`), `[0x27EB]` → run **decelerate** (`[0x7B1A]--`) [5977/59B1]
  - `[0x27E8]`, `[0x27E9]` → the remaining two (jump / up-down) — exact roles TBD from the decode.
  - The FSM packs 5 of them into a bitmask (`58FC-591F`) for state/animation selection.

## Player struct fields (Stage 0)
| field | addr | meaning |
|---|---|---|
| X | `0x4F1C` | world X (12.4 not — plain px; the velocity is 12.4) |
| Y | `0x4F1E` | world Y |
| tile col | `0x4F20` | `&0x1F` indexes the tile-property table (collision) |
| Xvel | `0x4F22` | X velocity (12.4 fixed; `>>4` per frame) |
| state | `0x4F24` | FSM state-ish (set to 3 in the boss seq) |
| facing | `0x4F25` | +1 / -1 |
| Yvel | `0x4F2A` | Y velocity (12.4 fixed; `>>4` per frame) |

## Recovered + shadow-verified (Stage 2)
- **`player_x_integrate` (1030:5A0F..5A33). VERIFIED** (`pre2/recovered/player.py`, `tests/test_player.py`;
  shadow 1999/1999 on L1 demo 102854 + 299/299 on L6). `new_x = X + sar(Xvel,4)`; commit only if
  `((cam_left+0x14)<<4) > new_x` and `8 <= new_x < 0xFF8` (signed) — else X unchanged (blocked). The player
  counterpart of the object `apply_velocity`. NOT live-hooked (shadow-only).

## Mismatch taxonomy (classes to watch as the FSM is recovered)
1. **Fixed-point**: 12.4 velocities, arithmetic `sar` (floor toward -inf) — sign/rounding bugs.
2. **Boundary clamps**: world edges `[8,0xFF8)`, camera right `(cam_left+0x14)<<4` — signed vs unsigned, off-by-one.
3. **Facing-dependent**: mirrored X offsets / animation by `[0x4F25]`.
4. **State-dependent**: the `cs:[0x7D2F]` per-state handlers change which fields move — recover per state.
5. **Input edge vs held**: some flags are level (held), the run counter integrates them — careful with
   accumulation across frames.
6. **Collision-coupled**: `5A96` (ground/tile) overwrites Y/Yvel after the integrate — recover the integrate
   and the collision separately and compose.

## Recommendation for the first live hook
`player_x_integrate` (`5A0F..5A33`) is the safest first live hook: it is an isolated INLINE block (no CALL/RET,
falls through to `5A36`), exactly the shape of the already-proven `object_velocity` hook. Wire it the same way
(read `[0x4F1C]/[0x4F22]/[0x8164]`, write `[0x4F1C]`, set `ip=5A36`), verify-mode at `5A36`, and confirm via
`hook_audit`. Because it changes the per-frame instruction count it is subject to the same demo-desync note as
`object_tick`. Recommend doing it as its own small, reversible step (with user confirmation) — NOT bundled
with deeper FSM recovery.
