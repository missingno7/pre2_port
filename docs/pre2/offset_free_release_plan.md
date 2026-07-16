# Offset-free release — the real flip

The object MODEL (`pre2/game`) is offset-free and provably detachable, but the shipped RUNTIME still executes the
recovered logic over the byte image via the offset-keyed `dgroup_view`. This plan finishes the flip so the
RELEASED `pre2native` runs on the object graph — no byte image, no offsets — with the byte-image serializer, VM,
replay and snapshot as an OPTIONAL attach-on-demand package.

## The requirement (architectural, from the user)

The released game must NOT execute against the DGROUP memory model. Gameplay code operates on named native state
and object references. All original addresses, byte-image layout, `rb`/`rw` access, serialization and VM
compatibility live ONLY behind a detachable verification bridge that is not shipped. Record-relative layout is
fine INSIDE the bridge, not as the gameplay abstraction. Target = the whole release dependency closure.

## The wall = the execution model, not just literals

The recovered engine is built on `rb`/`rw` reads returning offset-keyed `{offset:(value,width)}` write-contracts —
that IS the DGROUP memory model, threaded through every handler's whole call tree. Converting gameplay to "named
state + object mutation" is an EXECUTION-MODEL rewrite of the recovered layer, corpus-gated per function.

Honest execution surface (2026-07-14): **855 `rb`/`rw`/`wb`/`ww` call sites across 53 recovered files + native.**
That is the real ratchet to drive to 0 (the 192k figure was runtime accesses, inflated by loops; 855 is the source
surface). Approach: the object graph (DataclassBackend) already IS the state of record and provides `rb`/`rw` via
the swizzle, so functions can be converted to direct object access ONE at a time (function + its callers), each
byte-exact vs the corpus, while unconverted functions keep working through the swizzle. When the count hits 0, the
engine runs on named state; then `rb`/`rw`/layout/serializer/VM detach and the deploy DENY-lists them.

## Honest scoreboard (measured in the ACTUAL release closure — not just pre2/game)

The earlier `test_model_detached` gate only checked `pre2/game` (the model, which IS clean) and so misrepresented
the milestone. The RELEASE runs `pre2/native` + `pre2/recovered` + `pre2/views`, and those still carry offsets.
Two honest metrics, both must reach **0**:

| date | offset-const defs in the release closure | raw-logic offset accesses | notes |
|------|------------------------------------------|---------------------------|-------|
| 2026-07-14 | 742 (across 125 shipped files) | 192,631 | the true starting state; my "detached" claim covered only pre2/game |

Offsets concentrate in: dgroup_offsets 139, object_spawn 104, player_interaction 47, combat_interaction 36,
player 31, player_collision 26, + the views/* render layer. `python scripts/measure_name_frontier.py` tracks the
access count; a per-closure offset-const audit tracks the def count. Both ratchet DOWN only.

Per-module roadmap (ranked): object_spawn 69k, player_interaction 31k, player_collision 19k, object_tick 19k,
combat_interaction 11k, player 10k, input_decode 10k, object_inject 9k, loop 7k, camera_scroll 3k, others <2k.

## Stages

1. **Dissolve to 0** — recovered logic addresses state only by name/reference. Each module byte-exact-gated by
   `verify_player_dataclass` (5456 ticks) + `verify_object_finish` (lifecycle) + `verify_object_render`.
2. **Ship `NamedGameBackend`** (offset-free) + name-keyed views; the runtime builds+runs the object graph.
3. **Flip the deploy default** to the object store (DONE 2026-07-16, `a25acc1`); then remove the HISTORICAL
   image machinery from the release closure — `pre2/views/dgroup_view`, the layout, the serializer, and the
   historical oracle demo/snapshot loaders. **NOT because "the release loses replay/snapshot" — that was the
   wrong test** (corrected 2026-07-16; see `native_dataclass_lift.md`). A detached runtime may keep NATIVE input
   replay / native save states; what must go is anything that loads, constructs, requires or treats the DOS
   byte image as authoritative state. Deny-listing a feature is NOT evidence — the acceptance wall is physical
   impossibility: the game starts and plays with all historical-image modules unavailable. Attaching the bridge
   restores the oracle projection for verification.
4. **Gate**: extend `test_model_detached` to the whole shipped closure (no offsets, no bridge, no VM) and the
   deploy DENY so nothing leaks back.

## THE CRUX FINDING (2026-07-14, converting object_inject) — the ratchet can't reach 0 module-by-module alone

Converted `pre2/recovered/object_inject.py`'s full logic (project_entity + 8 handlers + second_pass_tick) to a
genuine offset-free implementation operating on real objects. **It cannot become the SOLE implementation yet**:
6+ committed tests legitimately drive the whole tick over a plain `ByteBackend` (the class-level default —
`test_cold_boot`, `test_faithful_golden`, `test_game_model`, `test_named_view`, `test_object_backed`,
`test_object_roundtrip`), plus the VM hybrid hook (`pre2/checkpoints/object_inject.py`) needs the byte calling
convention to hook live VM memory. So the pre-conversion implementation is KEPT (renamed `_bytes`), and
`native_object_system_step` dispatches on `state.backend`'s capability (duck-typed: real objects -> the new
pure path; plain `ByteBackend` -> the `_bytes` fallback). Both paths verified byte-exact (5456-tick corpus +
1579-tick lifecycle + 919 render frames + 986 pytest).

**The important, checked-explicitly consequence: neither `scripts/play_native.py` nor
`pre2/native/cold_boot.py` ever swaps `state.backend` away from the default `ByteBackend`.** So converting a
module's LOGIC — even perfectly, even proven byte-exact — does NOT by itself change what the deployed product
(`dist/pre2native`) executes. The new pure path only runs today under `DataclassBackend` (i.e., inside
verification scripts/tests that explicitly construct it — legitimately allowed, since that's the bridge's job).
**The literal 855-style ratchet count is therefore the WRONG single metric**: it can stay flat across genuine,
verified per-module conversions, because the dual-path keeps the old call sites physically present (renamed,
not deleted) until the boot-flip lands. Reducing it to 0 requires retiring the `_bytes` paths file by file,
which requires the boot-flip FIRST (see below) — not more per-module conversions in isolation.

## Stage 2.5 (NEW, now the priority): the boot-flip

For a per-module dual-path conversion to matter, `NativeGameState`'s runtime state of record must become the
object graph BY DEFAULT for the actual product loop — without `pre2.native`/`pre2.recovered` importing
`pre2.bridge` (the layering rule). The blocker: constructing the object graph from an image needs the offset
LAYOUT (`_ROUTES`, `ACTOR_LAYOUT`, ...), which is bridge-only.

Resolution sketched, not yet built: regenerate `pre2/native/boot_data.py` (currently a raw byte blob requiring
the layout to unpack) as a SHIPPED, generated Python module that constructs the INITIAL object graph directly —
`pre2.game.model.Player(x=.., ...)`, the initial `Actor`/`ArenaEntity` lists, etc. — as literal constructor
calls, with NO offset knowledge needed at import time (a one-time, bridge-side GENERATION step, analogous to how
`boot_data.py` itself was originally produced by a probe/extraction script — the OUTPUT carries no ongoing
offset dependency). `NativeGameState.__init__` then seeds `pre2.native.object_state.ObjectGraphStore` directly
from that generated module (still zero bridge import — the generated module lives in `pre2.native`), making the
object graph the ACTUAL DEFAULT for the real product. Once THIS lands, retiring each module's `_bytes` path
(and the ratchet genuinely reaching 0) becomes real, verifiable, one module at a time — each retirement gated by
the SAME corpus/lifecycle/render proofs, now exercised by the true default instead of a swapped-in backend.

This is the NEXT priority — bigger than any single module conversion, and the actual unlock for every
conversion done so far and still to come.
