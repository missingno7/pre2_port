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

## Recovered + LIVE (Stage 2 + 3)
- **`player_x_integrate` (1030:5A0F..5A33). LIVE + VERIFIED** (`pre2/recovered/player.py`,
  `pre2/checkpoints/player.py`, `tests/test_player.py`). `new_x = X + sar(Xvel,4)`; commit only if
  `((cam_left+0x14)<<4) > new_x` and `8 <= new_x < 0xFF8` (signed) — else X unchanged (blocked). The player
  counterpart of the object `apply_velocity`.
  - **Shadow** (pre-live): 1999/1999 (L1 demo 102854) + 299/299 (L6).
  - **Live hybrid**: installed at 5A0F, an inline-block swap that writes `[0x4F1C]` and jumps to 5A36;
    reproduces the ASM block's FLAGS (final `cmp`) + per-path instruction count (10/12/14/15). Fires ~2069×
    on L1 (alongside object_tick ~2130×).
  - **Verify-mode oracle**: 486/486 (L1) + 66/66 (L6), zero divergences (per-call ASM diff at 5A36).
  - Audit: classified `live`, verify-enabled, no drift. Demo byte-determinism is already affected upstream by
    the live `object_tick` collapse, so this hook is verified the desync-immune way (verify-mode + audit), not
    by demo byte-reproduction.
- **`player_y_integrate` (1030:5A36..5A3D). LIVE + VERIFIED**. `new_y = Y + sar(Yvel,4)`, UNCONDITIONAL (no
  clamps — the ground/tile collision at `5A96`, the very next call, corrects Y/Yvel on contact).
  - Shadow: 2069/2069 (L1) + 299/299 (L6). Live: inline-block swap (4 insns, writes `[0x4F1E]`, jumps to
    `5A41`, reproduces the `add` FLAGS). Fires ~2069× on L1.
  - Verify-mode oracle: 487/487 (L1) + 66/66 (L6), zero divergences. Note `5A36` is the X exit *and* the Y
    entry, so in verify mode the X verify-hook also captures the Y prediction (X always runs just before Y).
- **`player_tick_timers` (1030:5A47..5A87). LIVE + VERIFIED**. The routine tail: 7 byte + 1 word per-frame
  countdown timers (`[0x6BCE/6BCD/6BEA/6BE8/6BE4/6BE1/6C00]` + word `[0x6BE2]`), each a `sub [x],1 ; adc [x],0`
  saturating decrement (clamps at 0, NOT 0xFF).
  - Shadow: 2078/2078 (L1) + 299/299 (L6). Live: inline-block swap (17 insns, writes the 8 timers, jumps to
    the epilogue `5A8C`, reproduces the final `adc` FLAGS — otherwise dead, `pop bp` follows).
  - Verify-mode oracle: 483/483 (L1) + 66/66 (L6), zero divergences. Fires ~1996× on L1.

## The FSM dispatch (mapped via witness) — the next target
The kinematics shell (X/Y integrate + timers) is live. The actual behaviour is in the **per-input handler
table `cs:[0x7D2F]`**, dispatched at `5A0B` (`call word ptr [bx+0x7D2F]`).

**How `bx` is selected (witnessed):** `[0x6BC5]` is a *forced/scripted-animation* gate. In all normal
gameplay it is **0** (1997/1997 L1, 299/299 L6), so the setup at `5960` does `jmp 5A0B` and the momentum
block (`596A-5A0B`: run-counter `[0x7B1A]` accel/decel, `[0x6BC6]` deceleration, `[0x4F2A]` clamp) is SKIPPED.
`bx` then holds the **5-bit input bitmask** packed at `58FC-591F` (bits from `[0x27EC],[0x27ED],[0x27EA],
[0x27EB],[0x27E8]`), times 2. So the player FSM is **input-indexed**, not a stored-state machine.

**The index is the anim_id, not the raw bitmask.** The setup `5921-595C` maps the bitmask through a table to
the **anim_id** (the FSM state, 0..8): `anim_id = [0x7B7F + bitmask]` (forced to bitmask 0 when `[0x6BCD]`,
overridden to 8 when `[0x4F2D] >= 0x16`); on a state change it resets the run state (`[0x4F2C]=0`,`[0x6BEB]`);
then `bx = anim_id*2`. So at every handler's entry **`al`=anim_id and `bx`=anim_id*2** — exactly the args
`set_anim` consumes. (`player_select_anim_id`, recovered + shadow-verified 1997/1997 L1 + 299/299 L6.) The
bitmask→anim_id table `[0x7B7F]` (L1): `00 03 05 07 02 06 00 00 | 01 03 04 07 02 06 01 00 | 01 03 04 07 02 06
00 00 …`.

The `5A0B` call `call word ptr [bx+0x7D2F]` reads the pointer from **DS** (0x1A0F), not CS. The real
handlers (verified via post-call CS:IP), keyed by `bx`=anim_id*2, by frequency (L1):

| bx | handler | fires | role |
|---|---|---|---|
| 0x02 | `0x5EC4` | 856 | run |
| 0x00 | `0x5CDB` | 629 | idle / no input |
| 0x04 | `0x5F30` | 250 | |
| 0x10 | `0x5CCE` | 156 | |
| 0x06 | `0x5F96` | 58 | |
| 0x08 | `0x5E62` | 20 | |
| 0x0A | `0x5E96` | 16 | |

(The momentum block `596A-5A0B` and `484E` anim-select only run when `[0x6BC5]!=0` — a scripted/cutscene pose;
both are **dormant in normal play**, so they have no demo witness and are NOT recovery targets yet.)

**The handlers are thin compositions of shared physics/animation primitives** — the original source structure.
E.g. idle `0x5CCE` = `accel?; friction; advance_anim` (`62EC;6333;6374;638B`); run `0x5EC4` = inc a counter +
`accel(0x50); friction; set_anim; advance_anim` (`62B1;62EC;6374;638B`). The shared primitives:

| addr | primitive | effect |
|---|---|---|
| `62B1` | `player_accel(limit)` | Xvel += facing-step (when input held), clamp ±limit |
| `62EC` | `player_friction_dir` | Xvel -= `[0x6BF6]`>>3, floor -0x60 |
| `6333` | `player_friction_sym` | \|Xvel\| -= 0xC>>`[0x4F24]`, toward 0 |
| `6309` | `player_gravity(limit)` | Yvel += 0x10 (4 in water `[0x6BC7]`), cap at terminal |
| `635D`/`6374` | `set_anim_a/b(seq)` | load anim-sequence ptr `[0x4F28]` from table `[0x7CDF]` |
| `638B` | `advance_anim` | step `[0x4F28]`, write frame `[0x4F20]` (+facing bit) |

**Plan (collapses naturally):** recover the shared primitives first (pure, witnessed thousands of times), then
each handler is a few calls to them, then the dispatch is a table of handlers — `player_update` collapses into
one clean hook subsuming the X/Y/timer leaves.

**Proof the collapse works — handlers compose from the primitives.** Each handler (gate `[0x6BD0]==0` main
path) is a few primitive calls. Status of the 7 distinct handlers (anim_id 3/6/7 share `0x5F96`):

| anim_id | handler | fires (L1) | status | shape |
|---|---|---|---|---|
| 1 | `0x5EC4` | 856 | **recovered+verified** 795/795 | `sat_inc; accel(0x50); friction_dir; set_anim; advance_anim` |
| 5 | `0x5E96` | 16 | **recovered+verified** 14/14 | `set_anim; advance_anim; friction_sym; charge_6BCE` |
| 0 | `0x5CDB` | 629 | **recovered+verified** 719/719+88/88 | airborne/moving+trail(`5E11`)/default/long-idle/fidget(`0x79E0`); anim13+dust `3435/3414` unwitnessed |
| 2 | `0x5F30` | 250 | **recovered+verified** 288/288+4/4 | jump-arc table `0x79CE`/gravity + horizontal + `set_anim(2)` + 2× friction; `[0x6BE0]`→idle |
| 3/6/7 | `0x5F96` | 72 | **recovered+verified** 100/100+88/88 | "attack": set_anim/advance/friction/`[0x7B19]`/`[0x6BD0]`; sound path = `play_sfx`+trail+Yvel+projectile-spawn (`0x4F2E`); main path = render-sprite via phase frame-table |
| 4 | `0x5E62` | 20 | **recovered+verified** 11/11 | `[0x6BD3]=0;[0x6BE1]=4;charge`; `|Xvel|<=0x20`→accel(0x20)+set_anim+advance, else→idle (bx=8) |
| 8 | `0x5CCE` | 156 | **recovered+verified** 134/134 | `friction_dir; friction_sym; set_anim; advance` (al = post-friction Xvel low byte) |

Note: handlers `0x5CCE`/`0x5E62` call `set_anim` *after* `friction_sym`/`|Xvel|` clobbers `ax`, so their
`[0x4F27]` ends up velocity-derived (faithful, just not anim_id) — compose with the real register flow.

**Verification methodology note:** the standalone shadow probes hook a routine's entry+exit and run the
recovered fn from the live ASM state. Hooking a *high-frequency* shared instruction (e.g. idle's exit `5E0D`,
763×) lightly perturbs the deterministic demo clock, so per-handler call *counts* differ from the unhooked run
(idle dispatch: 629 real vs 719/763 under 2/12 hooks). The **byte-exactness of each compared call is still
valid** (it compares recovered vs real ASM from an identical state). The rigorous, non-perturbing oracle is
verify-mode (`enable_pre2_hook_verification`, instruction-count-transparent) on the eventual live
`player_update` — that is the final check before/as the collapse lands.

**The "attack" handler `0x5F96` IS the override tail `0x5F93`** — `5F93: mov al,[0x4F27]` falls straight into
`5F96`, so the override path is the attack body entered with `al=[0x4F27]` (vs `al=anim_id` for the attack
dispatch). Recovering `player_state_attack(al, bx)` closed BOTH gaps. It's audio+spawn+render coupled:
`play_sfx 0x282` (`sfx` return), a projectile spawn into the `0x4F2E` list (`627C` find-free), the override
flag `[0x6BD0]=(~[0x6BCF])&0x40`, and the render-sprite via the phase frame-table (`6081`). **Shadow-verified
byte-exact: 100/100 (L1) + 88/88 (L6), sfx stream matched.** Dead-value note: when the player render slot is
inactive (`[0x4F0E]==0xFFFF`) the ASM still fills `[0x4F0A]/[0x4F0C]` from a `play_sfx`-spilled register —
excluded as dead (never rendered), like the object_render scratch.

## First full FSM dispatch composed (`player_dispatch_handler`)
All 6 recovered handlers behind one uniform `(rb, rw) -> writes` entry, keyed by the `anim_id` from
`player_select_anim_id` (the recovered `cs:[anim_id*2 + 0x7D2F]` table). **Full-dispatch shadow (hook the real
`5A0B` dispatch, compare the whole contract at the `5A0F` return): 1980/1980 (L1) + 211/211 (L6), byte-exact,
zero divergences** across anim_ids 0,1,2,4,5,8. (This 2-hook probe perturbs far less than the per-handler ones:
a0=637 here vs 629 unhooked.) Remaining gaps, both fail-loud: the attack handler (anim_id 3/6/7, ~69×) and the
idle anim13 sub-path (`5D8A`, timeline-reachable but rare — its dust effects `3435/3414` are out-of-FSM-scope).

## Full FSM step composed (`player_fsm_step`) — front-end + select + dispatch
`player_fsm_frontend` (`58A7-591F`: flag combine + facing `[0x4F25]` + 5-bit bitmask pack; bitmask verified
1999/1999 vs the real `ah`) → `player_select_anim_id` → `player_dispatch_handler`. **Full shadow (capture at the
front-end `58A7`, compare the whole player contract at the dispatch return `5A0F`): 1980/1980 (L1) + 211/211
(L6), byte-exact, zero divergences.** The entire per-frame player FSM — input → state → behaviour — is recovered
code. (`DC1` input-decode + `6294` sync are upstream; `6294` is a no-op when `[0x280D]==0`.)

Two real composition bugs the shadow caught (not perturbation): the handler reads `[0x6BDB]` (input-held, for
`accel`) which the front-end writes — needed a full read-overlay of pending writes; and the `[0x6BD0]!=0`
override sends every handler (bar anim8) to the unrecovered tail `5F93` — now a fail-loud gap. Gaps total
101/88 per demo (override + attack 3/6/7 + idle anim13).

## Remaining to fully collapse `player_update`
1. **idle's `5D8A` anim13 sub-path = the "idle look-around"** — anim `0x13` + a CAMERA PAN (`3435` scrolls the
   camera right via `[0x2DE4]`/`[0x2DE8]`, `3414` left), triggered by `[0x27E9]` held while standing still
   (`[0x27E9]` is NOT in the dispatch bitmask, so it doesn't change anim_id — it just drives this branch).
   **UNWITNESSED in the whole demo corpus** (the running demos never idle-and-look) — needs a crafted/recorded
   demo before it can be recovered byte-exact; currently a fail-loud gap in `player_state_idle`. It also mutates
   cross-cutting camera state (`[0x2DE4]`/`[0x2DE8]`), so it's a small sub-island, not a leaf.
2. Live-hook `player_update` (front-end → select → dispatch → X/Y integrate → collision → timers) and collapse
   in **verify-mode** (the non-perturbing oracle), subsuming the X/Y/timer leaves. NB: the full-step *standalone*
   shadow shows perturbation-class residuals on the **stateful** attack sequence (`[0x4F28]` advances across
   frames; the attack handler itself is byte-exact standalone) — verify-mode is the authority there.
3. The collision sub-island `5A96`/`cs:[0x7D9B]`.

(Done since the last revision: the attack handler `0x5F96` + the override tail `0x5F93` — recovered + byte-exact
standalone; `player_dispatch_handler` routes anim_id 3/6/7 to it and `player_fsm_step` handles the override.)

## Other sub-island still ASM: collision + tile-interaction `5A96`
A genuine sub-island, not a leaf. Witness (L1): fires 2006×/frame; calls the tile-interaction worker `5B81` +
dispatches the **tile-type handler table `cs:[0x7D9B]`** *every* frame, runs a vertical tile-scan loop (`5CAC`
×3584), and the fall-off-edge path (`63B5`) ×820. Writes Yvel (868×), Y (39×), flags `[0x6BF3]/[0x6BD2]/
[0x6BE5]`. The `0x7D9B` table is the per-tile-type behaviour (ground stop, collectible, breakable, hazard…).

A single collapsed `player_update` hook (subsuming the X/Y/timer leaves, like `object_tick` subsumed
`object_velocity`) is the end state — reachable once the handler table + collision are recovered.

## Mismatch taxonomy (classes to watch as the FSM is recovered)
1. **Fixed-point**: 12.4 velocities, arithmetic `sar` (floor toward -inf) — sign/rounding bugs.
2. **Boundary clamps**: world edges `[8,0xFF8)`, camera right `(cam_left+0x14)<<4` — signed vs unsigned, off-by-one.
3. **Facing-dependent**: mirrored X offsets / animation by `[0x4F25]`.
4. **State-dependent**: the `cs:[0x7D2F]` per-state handlers change which fields move — recover per state.
5. **Input edge vs held**: some flags are level (held), the run counter integrates them — careful with
   accumulation across frames.
6. **Collision-coupled**: `5A96` (ground/tile) overwrites Y/Yvel after the integrate — recover the integrate
   and the collision separately and compose.

## Next, in order of value/tractability
1. **Shared FSM primitives** (`62B1/62EC/6333/6309` physics + `635D/6374/638B` anim) — pure, witnessed; then
   the handlers (`0x5EC4` run, `0x5CDB` idle, …) compose them. The FSM brain.
2. **Collision/tile-interaction `5A96` + `cs:[0x7D9B]`** — the largest piece; recover the tile-property lookup
   + Y/Yvel response, then the tile-type handlers one at a time.
Then collapse to a single `player_update` hook. Keep each its own small reversible step. No broad FSM rewrite;
no guessed struct fields. (`484E` anim-select + the `596A` momentum block are dormant in normal play — only
under the `[0x6BC5]` scripted-pose gate — so they wait for a witness.)
