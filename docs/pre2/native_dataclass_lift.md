# The native-dataclass lift — shipping the game on a real object model

The north star: the **released** game runs on a completely independent memory model made of plain Python
dataclasses (real fields — `player.x`, `rng.lcg_a`) with **no offsets and no DGROUP byte image in the live
path**. Because there is no byte image, the release build inherently cannot replay or snapshot — that loss is
the *sign* of true detachment, not a regression. The offset layout, the byte serialiser, replay, snapshot and
the VM oracle all live in the **detachable bridge** (`pre2/bridge/`, DENY-listed out of the deployed tree),
used only to verify the dataclass model against the DOS original (serialise → memcmp).

## Where we already are (verification foundation — DONE)

The object graph (`pre2/game/model.py`) is proven a **complete, byte-exact-equivalent state of record** at
every layer, via the bridge's `DataclassBackend` (offset↔field) + an injected controller:

- tick — `scripts/verify_player_dataclass.py` (5456 ticks / 116 demos, strict `readonly_image`)
- whole lifecycle incl. transitions — `scripts/verify_object_finish.py`
- every-frame render — `scripts/verify_object_render.py`
- the **real** interactive product loop (`native_frame_step_tagged`, injected `store`) —
  `scripts/verify_object_playloop.py`

That backend is a **verification mechanism**: it maps DGROUP offsets to dataclass fields, so it *needs* the
offset layout and therefore lives in the bridge. It can never be the shipped default (that would pull the
detachable workbench into the deployed tree — `deploy_native.py` DENYs `pre2.bridge` and the smoke asserts no
bridge module loads at runtime).

## The central tension

Shipped state access goes through the **offset-keyed views** in `pre2/views/dgroup_view.py`:
`airborne = _U8(0x6BF3)` — the offset is baked into shipped code, and every field descriptor reads
`backend.rb(base + off)`. The views are shared between the (current) image-backed path and any object path, and
the shipped default is image-backed — which *needs* offsets. So we cannot remove offsets from one view in
isolation without breaking the image path.

## The resolution — a shipped, name-keyed object backend

The endpoint needs a backend the product can ship that resolves state access **by name, with no offsets**:

- **Views become name-keyed.** `airborne = _u8()` — the descriptor captures its own attribute name via
  `__set_name__`; it carries width/sign (semantics) but **no offset**. `__get__`/`__set__` call
  `backend.read_field(view, name, width, signed)` / `write_field(...)`.
- **`NamedObjectBackend` (shipped, offset-free).** Holds the live `pre2/game` dataclasses and resolves
  `(view, field-name) → getattr(dataclass, name)`. The name→field map is pure names — no offsets — so it
  ships. This is a *different* backend from the bridge's offset-keyed `DataclassBackend`.
- **The offset↔name layout stays in the bridge.** It is used only to (a) serialise the dataclasses to a DGROUP
  image for memcmp verification, and (b) build the offset-keyed `ByteBackend` for the VM-oracle comparison.

Field-name parity between each view and its dataclass (already ratcheted to completion for the globals; proven
for Player/Actor/Rng) is the precondition — a name-keyed view field resolves iff the dataclass has that field.

## Phasing (each phase gated by the full verification ladder)

- **P1 — mechanism, additive (this slice).** Introduce the name-keyed descriptors + `NamedObjectBackend` in a
  new shipped module, prove the recovered RNG logic runs byte-exact through a name-keyed RNG view over an
  offset-free `Rng` dataclass, with the bridge serialising `Rng`↔bytes. Adds nothing to the shipped default;
  establishes the pattern.
- **P2 — convert views to name-keyed, cluster by cluster.** Each view class loses its offsets (moves them to a
  bridge layout table). Both paths keep working: the image path resolves names→offsets via the bridge layout
  during the transition; the object path resolves names→fields directly. Gated per cluster by the corpus.
- **P3 — flip the shipped default to `NamedObjectBackend`.** The product's live state becomes the dataclass
  graph; the image leaves the live gameplay path (render still materialises until P5).
- **P4 — delete the offset descriptors from shipped code.** `dgroup_view.py` carries names only; the offset
  numbers live solely in the bridge layout. `offset_name_lint` already guarantees no bare offset survives in
  accessor addresses; extend it to the descriptor definitions.
- **P5 — renderer reads dataclasses.** The renderer consumes `player.x` / `actor[i].sprite` instead of DGROUP
  offsets, so no image is materialised at all; the byte image exists ONLY in the bridge for verification.

## Verification ladder (unchanged, applied every phase)

1. round-trip identity — `to_image(from_image(img)) == img` on the named region;
2. tick-replay byte-exact vs the recorded VM digest across the whole lifecycle (transitions included);
3. the render/product-loop plane-equality proofs;
4. the three lints (offset_name_lint / offset_lint / lint) + the deploy smoke (no bridge module at runtime).

A wrong byte fails LOUD at an exact offset + tick.
