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
| `5C40` | per-type **update + collision dispatch** (`call [bx*2+0x7DA9]`) | **NO** — the state producer |
| `5C9E` | object-update handler dispatch (`call [bx*2+0x7DA5]`) + self-draw | **NO** |
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
- **RNG:** **OPEN** — the seed var / generator the handlers use is not yet located. First task of
  the recovery pass (a recovered handler can only be byte-exact if it consumes the same RNG stream).

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

1. **Locate RNG + the input vector** (seed var/generator; keyboard/state inputs the handlers read).
2. **Enumerate the per-type handler tables** `[0x7DA5]`/`[0x7DA9]` fully (count types, map each
   handler entry to its routine) — the object-type catalogue.
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
