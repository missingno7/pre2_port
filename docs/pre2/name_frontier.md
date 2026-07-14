# The name frontier — what's left for a whole-tick offset-free run

`scripts/measure_name_frontier.py` runs the gameplay tick on the bridge `DataclassBackend` (byte-exact) and
classifies **every** DGROUP access by its call site: a `dgroup_view` descriptor (a NAME-capable view — already
resolves via `backend.read_field` when present) versus RAW pointer arithmetic (recovered/native code calling
`rb`/`rw`/`wb`/`ww` directly with a computed offset — no name, so a pure offset-free backend can't resolve it).

The RAW set is the exact remaining "dissolve" work: each raw site must become a named record/array access (the
same conversion the `object_tick` / `combat` / `player_interaction` grinds already did for the pools they cover).

## Snapshot (368 ticks across 3 demos)

Not every RAW access is a dissolve target — a bulk region copy, a read-only table read, or a render-record
access is not "offset arithmetic to name." Categorising the raw set gives the HONEST denominator:

- **TRUE gameplay-logic frontier: 61.2% done** — `463,891` named / `758,023` nameable (`294,132` logic-raw left).
- Excluded (not a dissolve target): `firefly_sim` bulk blob (125k), `object_render`/`particles` render (47k),
  `game_tick_demo` harness (8k), `tables` loaded (0.6k) — `~181k` of raw that is legitimately not per-field logic.

The progression this session (whole-VIEW% and true-logic-frontier both climb as pools dissolve):

| after | VIEW-routed | true logic frontier |
|-------|-------------|---------------------|
| measured start | 37.7% | ~50% |
| object_particles | 48.3% | — |
| debris + popup | 49.4% | **61.2%** |

```
RAW-LOGIC by module — the REAL dissolve roadmap (only these are targets):
  110,283  object_inject.py        the variable-stride entity ARENA walk (flags1/stride/sprite_ref/skip/...) — HARD
   69,454  object_spawn.py         spawn + camera-script POINTER walks (partly irreducible pointer residue)
   31,144  player_interaction.py   object-list collision/pickup loops (mixed: some read read-only type-def tables)
   19,518  player_collision.py     the vertical-extent body-collision scan
   19,437  object_tick.py          object handler dispatch (mostly ObjectSlot already; some raw)
   11,336  combat_interaction.py   enemy-slot + bonus-cell scans
    9,859  player.py               player FSM word/timer fields
    9,594  input_decode.py         demo/idle input record
    7,372  loop.py                 the tick spine's own writes
    3,312  camera_scroll.py        scroll/camera followers
    1,107  object_particles.py     (residual scalars — the pool walk is DONE)
      874  camera_pan.py           camera pan
      738  effects_update.py       (residual scalars — the debris/popup/particle/projectile pools are DONE)
      104  audio.py                sfx pan/level

RAW not-a-target (excluded):
  125,460  firefly_sim.py   [bulk]    the 160-byte swarm-slot blob (serialised as a bytearray, not per-field)
   29,890  object_render.py [render]  runs over the materialised image by design (see verify_object_render.py)
   17,343  particles.py     [render]  the render-snapshot reader
    8,118  game_tick_demo.py [harness] _inject — the demo harness, not the shipped tick
      598  tables.py        [loaded]  read-only loaded lookup tables
```

## Reading the roadmap

- The offset counts are inflated by ARRAY walks: a pool scan touches each slot's distinct offset, but it reads
  a small VOCABULARY of fields per record. `object_inject`'s 766 offsets are really ~6 fields
  (`flags1`/`stride`/`sprite_ref`/`skip_flag`/...) over the arena's records — exactly what the name-keyed
  `NamedArrayView` / `EntityRecord` mechanism dissolves. So the *conceptual* frontier is far smaller than the
  raw offset count suggests.
- Two categories are NOT gameplay-state dissolve targets and can be excluded from the goal:
  - `tables.py` (loaded lookup tables) and the read-only bytecode/prop tables — loaded INPUT, not mutable state.
  - `object_render.py` — a RENDER concern; render runs over the materialised image by design (see
    `verify_object_render.py`), so its raw access is expected and fine.
  - `game_tick_demo.py:_inject` — the demo harness, not the shipped tick.
- Everything else (the pool/arena walkers) is index-addressable record work: the `NamedArrayView` mechanism
  (index, not `base + i*stride`) is the tool; the remaining effort is threading the recovered walks through it.

Re-run `python scripts/measure_name_frontier.py [max_ticks]` after each dissolve batch to watch VIEW% climb.
