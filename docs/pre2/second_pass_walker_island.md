# Second-pass walker island (1030:6913..698B)

Status: **LIVE — COLLAPSED (2026-06-28)**. This was the single biggest remaining in-game-logic chunk —
`1030:6900` was **~25%** of all interpreted instructions on a gameplay demo (hybrid-coverage audit,
2026-06-27). Now native: a single hook at `6913` (`checkpoints/object_inject.second_pass_tick_hook`) runs the
recovered `second_pass_tick` over live memory and does the `698B` ret. Byte-exact vs pure ASM (whole-memory
0-diff on gameplay demos; offline whole-pass shadow `pre2/probes/probe_second_pass_tick.py`; per-handler shadow
`pre2/probes/probe_second_pass_handlers.py`). Under verify-mode the hook steps aside so the `7F26`
per-projection oracle still fires.

All handlers recovered into `pre2/recovered/object_inject.py` (`dispatch_handler` + `second_pass_tick`);
the dispatch table `cs:[0x6AC3]`, the per-type handlers, and the walker loop are documented below.

## What it is

After `object_tick` (the main object-update walker, live-hooked at 684E, resumes at 6913), a SECOND pass walks
the variable-stride entity list at `0x8489` (the player + special entities: score popups, the player's
projectiles, etc.). For each non-skipped entry it dispatches a per-type handler that PROJECTS the entity into a
free slot of the main object list `0x4FD0` for rendering (via the live keystone `project_entity` 7F26), then
resolves its anim-frame descriptor.

The walker walks the WHOLE list each frame (most entries skipped by the early checks), which is why it dominates
the instruction count.

## Walker structure (6913..698B)

```
si = 0x8489
loop:
  [si]            stride byte; >= 0x32 -> end (ret)
  [si+2] == -1    -> skip (empty)
  [si+4] & 4      -> skip
  [0xB198]!=1 and [si+1]&0x80 -> skip
  bx = ([si+1] << 1) & 0xFF
  call cs:[bx + 0x6AC3]        ; the per-type handler (projects via 7F26); returns CF
  if CF==1: skip the anim-lookup
  lookup_anim_frame (6954-6981) ; resolve descriptor, store into the projected slot [+0xC] (di=[0xA32E])
  si += [si]                    ; advance by the stride
```

Entry record (variable-stride): `[+0]` stride, `[+1]` handler-index|flags(bit7), `[+2]` id word (−1 empty),
`[+4]` mode/flags, `[+9]` X, `[+0xB]` Y …

## Handler dispatch census (cs:[bx+0x6AC3], gameplay demos 190542 / 115215)

| bx | handler | fires | note |
|----|---------|-------|------|
| 0x02 | `7F26` | 48/18 | the keystone `project_entity` itself — **LIVE recovered** |
| 0x12 | `7E97` | 128/12 | thin-ish: `[+0x11]=0` → 7F26 → OR flags (level `[0x2D8A]`-dependent) |
| 0x18 | `7D1B` | 111/0 | player-proximity trigger (`[+9]/[+0xB]` vs player `[0x4F1C]/[0x4F1E]`, thresholds 0x140/0x280) |
| 0x14 | `7D9B` | 94/12 | **the player's second-pass handler** (the big one) |
| 0x04 | `7EE2` | 60/- | (7F26 wrapper) |
| 0x16 | `7D6E` | 48/16 | counter `[+7]` + 7F26 + `[+4]=0x37` + rng |
| 0x06 | `7ED8` | 47/12 | (7F26 wrapper) |
| 0x08 | `7EBF` | 46/- | (7F26 wrapper) |
| 0x0A,0x10 | `7EB5` | 32+16 | (7F26 wrapper) |

So ~9 handlers, all built on the live `7F26`. Several are thin wrappers; `7D9B` (player), `7D1B` (trigger) and
`7D6E` (counter) carry real per-type logic.

## What's recovered

- `project_entity` (7F26) + `find_free_object_slot` (806C) — **LIVE-hooked** (`checkpoints/object_inject.py`).
- `lookup_anim_frame` (6954-6981) — recovered (`object_inject.py`), **ASM_MATCHED** (a cold path: it only runs
  for on-screen-projected entities; the handlers return CF=1 / off-screen in the current demos, so it is not yet
  live-witnessed). 2 unit tests.

## ⚠ Witness finding (2026-06-27) — this island is witness-poor on its drawn paths

A census of the projection result (`7F26` carry) + the anim-lookup (`697D`) shows **the 2nd-pass DRAWN path
barely happens**:

- In every gameplay demo (190542/115215/105310/190645): `7F26` is called 50-370× but **draws 0 times** — every
  2nd-pass entity is off-screen/culled. So the handlers all take the trivial CF=1 (no-write) path, the
  anim-lookup never runs, and `7D9B` itself never draws.
- On snapshot `154531` (the only witness with draws): **only `7D9B` fires (15×) and draws (5×)** — no thin
  wrapper fires at all (no special entities present).

So: the thin wrappers' DRAWN path (project + mode) is **unwitnessed anywhere**, and the only witnessed-drawn
handler is the **complex** player trail projector `7D9B` (level-5/earthquake gates, a saturating counter
`[si+7]`, player-proximity tests vs `[0x4F1C]/[0x4F1E]`, a 16-entry position ring `[0xA341]`, terrain lookups
via `[0x7F5E]` + `es=[0x2DDA]`, its own `806C` projection — it does NOT call `7F26`). ~0xFC bytes of real logic.

**Implication:** the 25% is mostly the deterministic walk + skip + dispatch + stride-advance (well-witnessed,
all handlers returning CF=1), so the *loop* is cleanly composable + verifiable. But making the whole thing
byte-exact requires `7D9B` (its gate logic decides the CF even on the no-draw path) and ASM_MATCHED recovery of
the wrappers' drawn paths (or new witnesses with on-screen projectiles/special entities). `7D9B` is the
keystone of this island.

## Recovery plan (object_tick precedent)

1. **Recover the handlers** bottom-up. Most are thin `7F26` wrappers (verify the arg setup + the `[+4]` mode +
   the level-flag tweaks). Then the three with logic: `7D6E` (counter), `7D1B` (player-proximity trigger), and
   `7D9B` (player) — each shadow-verified.
2. **Compose the walker** (6913..698B): the list walk + skip predicates + dispatch (to the recovered handlers)
   + `lookup_anim_frame` + stride advance — over a read-through overlay, like `object_tick`.
3. **Live-hook** at 6913 (the `object_tick` resume point), with verify-mode coverage. The tiny dispatch +
   anim-lookup collapse to native; the ~25% at `1030:6900` goes away.

## After this
Remaining gameplay-logic chunks (same audit): `4Axx` particle logic (`4900`/`4A00`, ~11%), assorted `6xxx`
(`6100`/`6200`, ~8%), and unrecovered glue (`5400`/`5800`/`0700`, ~12%). Then the in-game logic loop is fully
native and the only ASM left in gameplay is the retrace spin (inherent).
