# The detachable umbilical cord

The end-user game and the "is it byte-exact against the original 1993 DOS binary?" machinery are two things
joined at exactly one seam. Plug the machinery in and the game is **absolutely verifiable** against the
original, tick for tick. Unplug it and the game **ships standalone** — no VM, no original-memory format, none
of the verification apparatus. The image is the plug; the names are the game.

## The two sides

### The game (ships)
- `pre2/recovered/*` — the gameplay logic, recovered from ASM, expressed as **named state** (`enemy.state`,
  `pv.x`, `g.energy`) — no raw offsets.
- `pre2/views/*` — the human-name ↔ layout declaration + the backends. `dgroup_view.py` keeps the offsets as
  the *evidence-annotated readable spec* (`airborne = _U8(0x6BF3)  # [asm 6401]...`), the game's single
  documented layout truth.
- `pre2/native/*` — the VM-less runtime. `field_runtime.py` runs the game on the **named field store**
  (`FieldBackend`); the byte image is only ever a load / render / serialise buffer.
- Ships with **no** `dos_re` (the VM), **no** `pre2/bridge` (the workbench), no probes, checkpoints, or game
  data. Machine-enforced by `scripts/lint.py` (shipped layers never import the workbench) and
  `scripts/deploy_native.py` (the deployed tree excludes them; the smoke proves it).

### The umbilical cord (detaches)
- `dos_re` — the pure-ASM VM, the ground-truth **oracle**.
- `pre2/bridge/state_fields.py` — the serialiser: the field-backed state ↔ an original-format DGROUP image
  (built on the machine-generated `pre2/bridge/field_registry.py`).
- `scripts/verify_*` — the harnesses that drive the two in lockstep and compare byte-exact digests.

## The one seam: `pre2/native/field_runtime.py`

```
to_field_store(state)   gameplay mode — named writes land in the field store, off the image
enter_image_mode(state) transition/level-load mode — that code mixes view + raw .data, so it runs wholly on
                        the image (materialise the field store in, use a plain ByteBackend for the duration)
materialize(state)      fold the field store's named bytes into the image (before a render / digest / snapshot)
```

The original DOS memory *format* lives entirely on the far side of this seam. Gameplay never needs it — it
reads and writes names. Only when something wants a contiguous original-format image (the renderer, a
snapshot, or the verifier comparing against the VM) does `materialize` produce one.

## Absolutely verifiable when plugged in — proven

- `scripts/verify_hybrid_tick.py` — every gameplay tick of the corpus runs on the field store, byte-identical
  to the reference: **FULL** for gorilla 919 / L6 207 / idle-fidget 187 / cold 15.
- `scripts/verify_hybrid_full.py` — the whole `boot → play → render` path on the field store, rendered planes
  included.
- `scripts/verify_hybrid_finish.py` — a **full playthrough across every transition** (level-end, respawn,
  teleport, game-over) on the field store, each tick checked against the recorded **pure-ASM VM digest**:
  **1579 ticks reproduced** across the level 8→9 boundary and its next level.

So: the field-backed game — running on names, needing no original-memory format at runtime — reproduces the
original ASM's DGROUP state exactly, across the game's entire lifecycle, whenever the VM oracle is plugged in
to check it.

## What "completely detachable for shipping" rests on

- The deployed tree imports none of the cord (lint + deploy DENY, smoke-proven).
- The game's state of record is the named field store, not a byte image (the whole-lifecycle proof).
- The only original-memory artefact that ships is the layout declaration in `dgroup_view.py` — kept because
  it is the *readable spec*, not because the runtime needs the numbers to be an image.

## Not (yet) done — and why it doesn't change the property
- `play_native`'s default runtime still uses `ByteBackend`, not `field_runtime`. This is an implementation
  detail: the shipped *code* is identical either way (named-state routing through a backend), and the
  detach/verify story is unchanged. Flipping the default (materialise before each render, mode-switch around
  transitions — the pattern `verify_hybrid_finish` proves) is optional polish, not a change to the umbilical.
- The offset numbers were deliberately **not** relocated out of `dgroup_view.py` — they are the evidence
  spec; a bare generated dict would lose the ASM anchoring (see `offset_quarantine_plan.md`).
