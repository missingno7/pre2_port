# Offset-free release — the real flip

The object MODEL (`pre2/game`) is offset-free and provably detachable, but the shipped RUNTIME still executes the
recovered logic over the byte image via the offset-keyed `dgroup_view`. This plan finishes the flip so the
RELEASED `pre2native` runs on the object graph — no byte image, no offsets — with the byte-image serializer, VM,
replay and snapshot as an OPTIONAL attach-on-demand package.

## The wall

The recovered logic still does raw pointer arithmetic (`rb(si + 9)`, `for i: si = base + i*stride`). That needs an
offset→field map to resolve, so *something* with offsets must ship to run it. To detach the offset layout, that
arithmetic must become name/reference access (`slot.field`, `for slot in actors`). This is the **dissolve**; the
runtime flip is all-or-nothing (an offset-free backend can't resolve a single raw offset), so the dissolve must
reach **0 raw-logic** before the flip works.

## Driver: the raw-logic ratchet (target 0)

`python scripts/measure_name_frontier.py` reports `logic` raw-access count. It must only ever go DOWN.

| date | raw-logic | notes |
|------|-----------|-------|
| 2026-07-14 | 192,631 | baseline at the start of the flip (74.6% of the nameable surface done) |

Per-module roadmap (ranked): object_spawn 69k, player_interaction 31k, player_collision 19k, object_tick 19k,
combat_interaction 11k, player 10k, input_decode 10k, object_inject 9k, loop 7k, camera_scroll 3k, others <2k.

## Stages

1. **Dissolve to 0** — recovered logic addresses state only by name/reference. Each module byte-exact-gated by
   `verify_player_dataclass` (5456 ticks) + `verify_object_finish` (lifecycle) + `verify_object_render`.
2. **Ship `NamedGameBackend`** (offset-free) + name-keyed views; the runtime builds+runs the object graph.
3. **Flip the deploy default** to the object store; DENY-list `pre2/views/dgroup_view` + the layout + the
   serializer + demos/snapshots so they drop out of the dist. The release loses replay/snapshot (the SIGN of
   true detachment); attaching the bridge restores them for verification.
4. **Gate**: extend `test_model_detached` to the whole shipped closure (no offsets, no bridge, no VM) and the
   deploy DENY so nothing leaks back.
