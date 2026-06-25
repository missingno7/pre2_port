# Object system island — scope & verification contract (recon, do NOT replace yet)

The renderer can now *display* the game (clean-framebuffer composition complete — see
`renderer_status.md`). This island is the next deep root: **who creates the displayed object
state**. Per the standing plan this document **scopes** the object system — maps its inputs,
outputs, side effects, records, scratch, RNG, particles, triggers, camera and audio/render events,
and defines a **strict verification contract** — *before* any part is made authoritative. The
original ASM stays the oracle throughout.

## Correction: `65A0`/`8BFF` are profiling hot-IPs, not routine entries

The "object-draw loops `65A0`/`8BFF`" in `renderer_island.md` are *sampled hot instruction
pointers*, not entries. Capstone-confirmed:

- The routine whose **ret is `65AE`** (9-register frame: `push ax,bx,cx,dx,di,si,ds,es,bp`) is an
  **object-draw primitive** (sibling of the recovered `653D`): from `di` = tile pos (`dh`=row,
  `dl`=col) it computes the screen dest offset (`(row /12)·stride·16 + (col%20)<<shift`, the
  `0x50/2` vs `0x28/1` stride/shift chosen by `cs:[1]` mode; `call 453B` when mode≥3), calls the
  blit wrapper `3B77`, sets `[0x6BBD]=1` (animated-tiles-present). Hot-IP `65A0` = its `[0x6BBD]=1`
  / epilogue.
- The routine whose **ret is `8BF5`** is the same draw primitive **plus** it forces a full grid
  redraw: `[0x2DF4]=1` (dirty) and `[0x2DE0]=0x55AA` (the redraw sentinel). Hot-IP `8BFF` is in the
  adjacent scale helper `8BF6..8C12` (`[di+3]` → `<<4` → `[0xA336]/[0xA338]` scale scratch).
- These draw primitives are selected per object type by a small pointer table at `DGROUP:0x7DA9`
  (`call word ptr [bx+0x7DA9]`; entries seen: `0x6672`, `0x6673`, `0x65AF`).

So the *render* side of the object system is blit-backed (the blit itself is already recovered).
The **state-producing** side is the update/collision dispatch below.

## Sub-system map

| Addr | Role | Recovered? |
|---|---|---|
| `26FA` | active-sprite-list **renderer** (cull/animate/position/clip + blit) | **VERIFIED** (`object_render.py`) |
| `653D` | object-draw **primitive** (cull vs camera + dest-off + blit) | RECOVERED (`object_draw.py`, dormant) |
| `65xx` (ret `65AE`) / `8Bxx` (ret `8BF5`) | draw-primitive **variants** (stride/mode + force-redraw) | mapped here (render side, blit-backed) |
| `5406` | **ObjectSlot structure** draw loop (multi-tile, proximity-triggered) | partially (calls `653D`) |
| `5C04` | **PLAYER** movement-state dispatch (`call [bx+0x7D9B]`, idx=player state) | **NO** — player state producer |
| `5C33` | **DRAW**-primitive selector by tile-type under the player (`call [bx+0x7DA9]`, idx=tile_attr&0xF) → `0x6672`/`0x6673`/`0x65AF` | render side (blit-backed) |
| `5CAC` | tile-collision response dispatch (`call [bx+0x7D95]`, idx=tile attr 2/4) | player tile reaction |
| `5C9E`/`5CCE` | **PLAYER** per-frame orchestration (`call 0x62EC` gravity / `0x6333` input / `0x6374` collide / `0x638B` anim) | **NO** — player update |

### GENERAL OBJECT-UPDATE SYSTEM — LOCATED (2026-06-23)

Found empirically: a write-watcher on the active-list range during gameplay showed the position
writers (`+0` X / `+2` Y) at `6869`/`6875` etc. — the walker entered from the main loop at ~`6822`.

**The object-update walker (`~6840..690A`):**
- base `si = 0x4FD0`, **count `bp = 0xC` (12 slots)**, **stride `0x12` (18 bytes)** (`690A add si,0x12`).
- per slot (skip if `[si+4]==0xFFFF`): apply **Y velocity** `[si+2] += [si+0xA]>>4`, **X velocity**
  `[si] += [si+8]>>4` (unless `[si+8]==0xFFFF`); advance the **animation script** `[si+0xC]` → write
  the sprite id+frame `[si+4] = (id & 0x6000) | frame`; then **dispatch the per-type handler**.
- **Object record (0x4FD0, 18 bytes):** `[+0]`X `[+2]`Y `[+4]`sprite-id|flags(0x6000)|anim-frame
  `[+6]`→type-DEF pointer `[+8]`X-vel `[+0xA]`Y-vel `[+0xC]`anim-script ptr `[+0xE]`state/sub-timer
  `[+9]/[+0xB]`aux `[+0x11]`life. (The player is the separate FSM; this list is enemies/objects/effects.)
- **Type definition (at `[si+6]`):** `[+1]` = handler index, `[+4]` = behavior flags. The handler
  dispatch is `68FC: call cs:[bx + 0x6AA9]` with `bx = [def+1]*2`.

**Type catalogue — `CS:0x6AA9`, 24 handlers** (`0x75C4..0x7F6C`; most call the shared helper `0x8084`):
types 0..23 → `7C90 7C8C 7C2D 7B91 7ADF 7A60 78EC 7898 77DE 773D 7665 760F 75C4 7F6C 7F26 7EE2 7ED8
7EBF 7EB5(×4) 7E97 7D9B`. These are the enemy/object/effect AI handlers (read `[si+0xE]` state, set
`[si+4]` sprite, check collision via `0x8022`/`0x8084`).

**Second object list (`6913..`):** base `0x8489`, dispatch `6944: call cs:[bx + 0x6AC3]` (idx `[si+1]`)
— a separate (≤0x32) list, likely effects/particles. To inspect when needed.

NEXT (with the do-not-assume discipline): **identify the "500" score-popup's specific type/handler**
by finding its live slot in a witness (its sprite renders as digits + it floats up = negative Y-vel
`[si+0xA]`), confirm it is small + low-side-effect, then split spawn/init vs score-add vs float vs
anim/blink vs expiry vs draw-entry and recover the update handler in shadow. Do NOT assume the score
add is in the popup handler (likely at collect-time, in the collectible/collision handler).

### Map correction (2026-06-23): `0x7D9x` is the PLAYER state machine, NOT the object catalogue

Enumerating `0x7D9B` and disassembling its cluster (`0x652C..0x6680`) showed these are **player**
handlers — they do player physics (`es:[di+0x100]` tile-collision xlatb, position `[0x4F1E]`, facing
`[0x4F24]=0..3`) and `0x65AF→0x65B3` is the **player death** handler (dec lives `[0x27D8]`, reset
energy `[0x27D6]`, set `[0x6BE4]=2`). The three tables share one pointer array (`0x7D95` tile-reaction,
`0x7D9B` movement-state, `0x7DA9` draw-by-tile). So the player is a self-contained FSM. The general
**enemy / pickup / "500" popup** updates live in a SEPARATE main-loop system (one of `6822`/`6210`/
`60FE`/`4907`/`5850`, classified as "other game systems" in `renderer_island.md`) that walks the
active-sprite list — **still to locate**. This correction avoids recovering the wrong handler (the
user's "do not assume" caution).
| `4b8e` | particle/effect system | **NO** (NEEDS-REPRO, watch-list) |
| `3721` | tile-flag trigger | **NO** |
| `3922` | auto-scroll script | **NO** |

## Object data model

- **Active-sprite list** `[0x4F0A .. 0x5720]`, **18-byte** records, walked top→down (`si=[0x2DEE]`,
  `-=0x12`). Fields: `[+0]` world X, `[+2]` world Y, `[+4]` sprite id (`-1`=empty), `[+5]` flags
  (bit5=drawn; H-flip), `[+0x11]` anim/life counter. Attribute tables by `id<<1`: width `[0x7190]`,
  draw offsets `[0x752A/B]`, sprite-data seg `[0x62E8]` / off `[0x5F48]`. (All already consumed
  read-only by the recovered `26FA` renderer.)
- **ObjectSlot table** `0x83EF`, **15 slots × 10 bytes**: `[+0]` word draw pos (decremented),
  `[+2]` `dl` width(tiles), `[+3]` `dh` height(tiles), `[+4]` word key (`0xFFFF`=empty,
  `0xFFFE`=triggered), `[+6]` word data ptr (seg `[0x2871]`), `[+8]` 2 bytes (TBD).
- **Player/object kinematics** (DGROUP scratch seen in `5C40`): position/velocity `[0x4F18]`,
  `[0x4F1A]`, `[0x4F1C]`, `[0x4F1E]` (X), `[0x4F22]` (Y); collision via a tile lookup
  `es:[di+0x100]` + `xlatb` against the level map.

## Side-effect map (the dimensions to pin before recovery)

- **Inputs:** active list + ObjectSlot table; camera `[0x2DE0..0x2DE6]`; level collision/tile map
  (`es:[di+0x100]` xlatb); player kinematics `[0x4F18/1A/1C/1E/22]`; mode `cs:[0]`/`cs:[1]`; input
  state (keyboard); the per-type handler tables `[0x7DA5]`/`[0x7DA9]`.
- **Outputs — object records:** the 18-byte active records (`[+5]` drawn bit, `[+0x11]` life) and
  the 10-byte ObjectSlots (`[+0]` pos, `[+4]` key transitions `FFFF→FFFE`).
- **Outputs — render events:** blit to A000 VRAM (via `3B77`/`3B58`); `[0x6BBD]=1`
  (animated-present); `[0x2DF4]=1` (dirty) + `[0x2DE0]=0x55AA` (force-redraw sentinel); scale
  scratch `[0xA336]/[0xA338]`.
- **Scratch:** sprite-blit scratch `cs:[0x26E0..0x26F7]`; `[0xA336/A338]`; `[0x4F1C]` collision
  accumulator.
- **Camera effects:** shake magnitude `[0x6BEA]` (apply now recovered — `camera_shake.py`); the
  redraw sentinel.
- **Triggers:** proximity pre-pass sets ObjectSlot key `0xFFFE` + `[0x6BE6]=7`; tile-flag trigger
  `3721`.
- **Audio events:** `play_sfx` @ `1030:0282` (dl=SFX index) — handlers fire SFX on hit/pickup.
- **Particles:** `4b8e` — **OPEN** (NEEDS-REPRO); determine if it spawns into the active list
  (already composed) or owns its own blit.
- **RNG: RESOLVED — there is NO RNG in the gameplay path (the game is deterministic).** Checked
  2026-06-23: over 900k steps of executed gameplay (2862 unique code sites, pure ASM) there are
  **0** LCG `imul`-with-immediate and **0** `int 1A` (the only raw `CD1A` byte match, 0x9179, is a
  mid-instruction false positive in a planar-copy loop). The 6 `mul`/1 `div` are the renderer's tile
  math. Corroborated by the project's already byte-reproducible demos (a timer-seeded RNG would break
  that). Any pseudo-variety is a deterministic function of the frame counter `[0x6BD5]` / position.
  CONSEQUENCE: handlers are deterministic functions of (state, input, frame counter) — no RNG stream
  to track in the verification contract.
- **Input vector (located 2026-06-23):** the keyboard ISR **int 9 @ `1030:1820`** reads port 0x60,
  stores the raw scancode at **`[0x2874]`** and bumps an event counter **`[0x2877]`** (make/break via
  scancode bit 7). The player-control handler reads the resulting held-key state; the popup/effect
  handlers read no input. Exact held-key state vars: map when recovering the player handler.

## Verification contract (define before authoritative)

The object update is a *whole-state* mutation, so the contract is the **complete object state +
render side effects**, not a hand-picked subset:

1. **Records:** after each update-dispatch boundary, the full active list `[0x4F0A..0x5720]` + the
   ObjectSlot table `0x83EF` (15×10) + the kinematics vars must equal the ASM's, byte-for-byte.
2. **Render side effects:** `[0x6BBD]`, `[0x2DF4]`, `[0x2DE0]`, the VRAM region the blit touched.
3. **External events in order:** the sequence of `play_sfx` calls and trigger transitions.
4. **Mechanism:** the existing **whole-memory `--full-verify`** oracle (nothing leaks — it diffs the
   complete machine state after each recovered routine) is the right tool; promote per-handler
   contracts to semantic diffs only once each handler is recovered. Plus a **demo co-sim**: drive a
   recorded demo and assert recovered object state == ASM at every frame (the deterministic demo
   clock already exists).

## Phased recovery order (each phase: recover pure → shadow-verify → only then authoritative)

1. ~~**Locate RNG + the input vector**~~ — **DONE** (2026-06-23): no RNG (deterministic); input
   vector = int 9 @ `1030:1820` → scancode `[0x2874]` + counter `[0x2877]`.
2. ~~Enumerate `[0x7D9B]`~~ (= PLAYER FSM) + ~~locate the general object-update system~~ — **DONE**
   (see "GENERAL OBJECT-UPDATE SYSTEM — LOCATED" above): walker at `~6840` over the `0x4FD0` list (12
   slots, stride 0x12) dispatching the 24-type catalogue at `CS:0x6AA9` by `[def+1]`. NEXT: identify
   the "500" popup's specific type/handler in a witness, confirm it is the smallest safe one.
3. **Recover one simple handler** (e.g. a static pickup or a popup like the "500" score sprite)
   pure, shadow-verify its record + SFX + render contract over a demo (the `advance_animation` /
   `camera_shake` ownership pattern), ASM still oracle.
4. **Recover the collision/kinematics core** (`5C40` probe) — the shared movement primitive.
5. **Work outward** to enemies/player, then particles `4b8e` (with a witness) and triggers `3721`.
6. Only after a phase's shadow proof bakes: make it authoritative (the active list is then produced,
   not read), shrinking the hook surface toward a self-driving loop.

## Principle

The faithful layer stays byte/state-verifiable against the oracle at every phase. Do not wire the
renderer to a half-recovered object layer and call it faithful — recover + verify each handler
*before* it feeds the renderer. (Standing rule: shorten the coastline upward; never grow a guess.)

## Stage 0/1 results (2026-06-26) — boundary CONFIRMED + first leaf recovered

Boundary disasm-confirmed (capstone on dumped bytes) + dynamic probe `pre2/probes/probe_object_tick.py`
(observe-only, chaining hooks; replays a demo). **The object-update walker `1030:684E..6913`:**

- `684E` init: `si=0x4FD0`, `bp=0xC` (12 slots), `cl=4` (velocity shift). `6856` loop top: `ax=[si+4]`;
  `0xFFFF` → skip to `690A`. `690A`: `si+=0x12`, `dec bp`, loop to `6856`; exit `6913`.
- **velocity apply `6861..6873`** — `[si+2] += sar([si+0xA],4)` (Y, always); `[si+8]!=0xFFFF` → `[si] +=
  sar([si+8],4)` (X). RECOVERED `object_update.apply_velocity`, **VERIFIED 770/770 + 453/453 exact** vs ASM
  across two demos (moving + static). Note: X-vel `-1`==`0xFFFF`==the sentinel (X-vel -1 is unrepresentable).
- **anim advance `6881..68E6`** — walk script ptr `[si+0xC]` (negative entry = relative back-jump = loop);
  `dx+=0x138` frame base; `[0x6BE2]`/`[0xA801]` scale-region adjust; store advanced ptr `[si+0xC]`; write
  `[si+4] = ([si+4]&0x6000) | (frame | flip-bit from [si+9]&0x80)`. (next recovery candidate)
- **handler dispatch `68FC`** — `bx=[def@[si+6] +1]<<1`; push 9 regs; `call cs:[bx+0x6AA9]`; pop. Per-type AI.

**LIVE vs STALE slots** (demo 001513): only **slots 0 & 1** live (non-empty + moving: 470/132 and 300/297
ticks); slots 2..11 always empty (`[+4]==0xFFFF`). The walker self-marks empties — a slot is valid IFF
`[+4]!=0xFFFF`. (This is why static-snapshot reads of the *renderer's* list showed garbage: never trust a
slot without the live `0xFFFF` check.)

**CONFIRMED-LIVE fields** (written by the walker, observed): `[+0]`X, `[+2]`Y (velocity apply); `[+4]`
sprite-id|frame, `[+0xC]` anim ptr (anim advance). **Read-as-input:** `[+6]` def ptr, `[+8]`/`[+0xA]` vel,
`[+9]` flip. **Do not name yet** (not observed written here): `[+0xE]`, `[+0x11]`, `[+9]/[+0xB]` aux.

**Handler dispatch map** (demo 001513, idx = `[def+1]`): `1→7C8C` (id 0x13d), `2→7C2D` (0x13e/f),
`10→7665` (0x187..0x18a) — matches the `CS:0x6AA9` catalogue. Other demos will surface more types.

**Recommendation — first safe live replacement:** `apply_velocity` (6861..6873). It is tiny, isolated, pure
(reads `[+8]/[+0xA]`, writes `[+0]/[+2]`), has NO other side effects, and is proven 770+453/453 exact. A live
hook there is the lowest-risk first authoritative object-system routine. Anim-advance (`6881..68E6`) is the
next leaf (more state: the script-pointer walk + `[0x6BE2]/[0xA801]` reads), recover in shadow before live.

## Stage 1 cont. (2026-06-26) — anim-advance recovered + handler substructure mapped

- **`1030:6881..68E6` — `advance_animation`. VERIFIED** (`object_update.advance_animation`,
  `tests/test_object_update.py`). Walk the script ptr `[si+0xC]` (a DS-relative offset list of frame words;
  a NEGATIVE word is a relative back-jump = animation loop); `frame = ((raw & 0x1FFF) + 0x138) & 0x1FFF`;
  `[si+4] = (old & 0x6000) | frame | (flip<<15)` (flip from `[si+9]` bit7); advance ptr +2; write the
  `[0xA340]` scratch byte `((raw>>8)&0xE0)|scale`. Shadow-proven 770/770 + 447/447 exact (two demos, incl. the
  `[0xA340]` side effect). The `scale` ([0x6BE2]) region-remap (boss zoom, 0xA801 table) is GUARDED
  (ObjectScaleUnsupported) — never fired in normal-gameplay demos (0 skips).

Handler substructure mapped (disasm), charting the path to lifting the whole walker:
- **`0x8084` — the keystone "despawn-if-far-from-player" pre-check** every handler calls first: `|obj.x −
  player[0x4F1C]| > 0x140` or `|obj.y − player[0x4F1E]| > 0x12C` → despawn (`[si+4]=0xFFFF`, `[def+4]&=0xFB`,
  `[def+7]=0`); a state `[si+0xE]>=0xA` far object jumps to `0x7CFF` instead. Recover next.
- **`0x698C` — object-vs-tile collision helper** (called when `[def+4]&8`): `tileY=[si+2]>>4`, `tileX=[si]>>4`,
  index the level map `es:[0x2DDA]` at the object tile + the tile in the X-vel direction, xlat table `0x7E5E`.
- **handlers (`CS:0x6AA9`) are thin**: idx1 `7C8C` = `call 0x8084; ret`; idx0 `7C90` = `call 0x8084` +
  pickup logic (sets collide flag `[def+4]|=8`, state `[si+0xE]`). Most delegate to `0x8084`/`0x8001`/`0x8022`.

LIFT PATH (toward a high-level `object_tick` in real source): velocity ✓ + anim ✓ recovered; next the shared
helpers `0x8084` (despawn) → `0x698C`/`0x8001`/`0x8022` (collision) → the thin per-type handlers → then
compose the walker loop (684E..6913) as one function with the recovered leaves (handlers as a dispatch table),
shadow-verify the whole tick, and live-hook the composed walker (not each tiny leaf — coastline upward).

## Stage 1 cont. (2026-06-26) — despawn-if-far keystone recovered

- **`1030:8084` (+ the `7CFF` tail) — `despawn_check`. VERIFIED** (`object_update.despawn_check`). The shared
  pre-check every AI handler calls first: keep the object when state `[+0xE]==0xFF`, or it is drawn
  (`[+5]&0x20`), or within `FAR_X`×`FAR_Y` (0x140×0x12C) of the player (`[0x4F1C]/[0x4F1E]`, abs16 distance);
  else despawn — `[+4]=0xFFFF`, `[def+4]&=0xFB`, `[def+7]=0`; a far `state>=0xA` object additionally frees its
  spawn slot `[def+2]=0xFFFF` (unless `[def+4]` bit1 set, via the 7CFF tail). Shadow-proven 770/770 + 234/234
  exact (incl. the real despawn writes). tests +7.

Recovered object-update leaves so far: **apply_velocity ✓ (live) · advance_animation ✓ · despawn_check ✓**.
Remaining for the walker lift: the collision helpers (`0x698C`/`0x8001`/`0x8022`) the handlers call, then the
thin per-type handlers, then compose `object_tick` (684E..6913).

## Stage 1 cont. (2026-06-26) — on_screen_tile + helper map

- **`1030:8022` — `on_screen_tile`. VERIFIED** (the visible-window predicate the AI handlers use most —
  5530 + 6745 fires). Pixel→tile `>>4`, SIGNED camera-relative offset in `[-2,22]×[-2,13]` → CF. Shadow
  5530/5530 + 6745/6745 exact. tests +5.

Fire census (demo 001513): `8022`=5530, handlers `7C8C`=310/`7C2D`=300/`7665`=160, `698C`=141, `8058`=7,
`806C`=5, `8048`=2, `8001`=0. So `698C` (object↔terrain collision) and the per-type handlers are the
remaining walker pieces with witnesses; `8001` needs a witness (its handler `7C90` doesn't run here).

Recovered object-update leaves: **apply_velocity ✓(live) · advance_animation ✓ · despawn_check ✓ ·
on_screen_tile ✓**. NEXT: `698C` is the big terrain-collision routine (own pass + a collision witness); then
the per-type handlers `7C8C`/`7C2D`/`7665` (thin: despawn + on_screen + small logic) → compose the walker.
