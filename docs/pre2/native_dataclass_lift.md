# The native-dataclass lift — shipping the game on a real object model

The north star: the **released** game runs on a completely independent memory model made of plain Python
dataclasses (real fields — `player.x`, `rng.lcg_a`) with **no offsets and no DGROUP byte image in the live
path**. The offset layout, the byte serialiser, the historical oracle replay and the VM all live in the
**detachable bridge** (`pre2/bridge/`, DENY-listed out of the deployed tree), used only to verify the dataclass
model against the DOS original (serialise → memcmp).

> **INVARIANT — corrected 2026-07-16.** This doc used to say the release "inherently cannot replay or snapshot,
> and that loss is the *sign* of true detachment". **That was stated too broadly and is not the test.** A
> genuinely detached native game may still support deterministic input replay, save states, debugging
> snapshots and regression recordings — their existence implies nothing about dependence on the DOS memory
> model. The meaningful distinction is what they *speak*:
>
> * **historical** replay/snapshot — serialises, restores, injects into, or compares a DOS byte image;
> * **native** replay/snapshot — serialises native input events or the authoritative object graph.
>
> **The actual invariant: the release runtime cannot load, construct, require, or treat the historical DOS
> memory image as authoritative game state.** Removing a replay feature is *not* evidence of detachment; do not
> use a deny-list entry as proof. See "P5 acceptance wall" below — success is physical impossibility.
>
> Canonical rule: *detachment does not mean losing replay or save states; it means replay and save states no
> longer speak the language of the original DOS memory image.*

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

## CORRECTION (2026-07-16) — read before acting on the phasing below

Two things this doc's P2/P3 imply turned out to be wrong in practice. Both were learned the hard way; the
phasing below is otherwise still sound, but **do not start P2 work without reading this.**

1. **P3's "`NamedObjectBackend`" already exists, under a different name, and it is NOT the one described here.**
   The shipped, offset-free, name-resolving store the product should default to is
   **`pre2/native/object_state.py`'s `ObjectGraphStore`** — already shipped, already proven byte-exact for the
   whole lifecycle (`verify_object_finish`, 1579 ticks across transitions) and for the real product loop
   (`verify_object_playloop`, 919 ticks). It is also the only mechanism with a *completeness* proof:
   `readonly_image=True` raises on any un-routed mutable write. `pre2/views/named_view.py`'s
   `NamedObjectBackend` (P1's exemplar) is a proof-of-mechanism, not the destination.
   **The real remaining gap is not "build a backend" — it is that `ObjectGraphStore`'s offset-aware
   CONSTRUCTION lives in `pre2/bridge/game_layout.py`, which shipped code may not import.** That, and the
   boot-flip that follows it, are specified in `offset_free_release_plan.md` §"Stage 2.5 — the boot-flip",
   which is the actual next step.

2. **`FieldRegistry` (the `dgroup_view.py` name-registry used to make `state.rng` and `state.player` live) has a
   structural ceiling: it intercepts `read_field`/`write_field` only, never `rb`/`wb`.** So the moment ANY
   offset access to a registered cluster's bytes remains, the live object and the image split-brain. Registering
   `PlayerView` globally silently broke one-shot event paths (game-over reset, cave-teleport, attract-title)
   that write a player field and read `.data` back immediately with no sync point — 14 tests caught it. Player
   therefore ships *transactional* (re-seeded from `.data` per pass, folded back immediately; the image stays
   authoritative). **That pattern does not scale and is not the endpoint — do not extend it to more clusters.**
   It is superseded by the boot-flip: once `ObjectGraphStore` is the default, `ObjectGraphStore.player` IS the
   live object and the registration becomes redundant. See the `pre2-player-p2-live-wiring` memory.

Also worth carrying: **`_ROUTES` is NOT derivable from the `dgroup_view` descriptors** (they are overlapping
siblings over one truth — the ASM — not derivations). `_ROUTES` carries the *grouping* of offsets into dataclass
instances, the *dataclass field name* (differs from the view name in 18+ scalar and ~380 array cases), and the
*canonical width* where views declare byte+word aliases for one address. So moving it is a MOVE, not a
generation.

## Honest status (2026-07-16, after the Stage 2.5 boot-flip `a25acc1`)

State only what is proven. **Do not describe the current shipped build as fully detached or memoryless.**

| | |
|---|---|
| RE workbench + emulator absent from the deployed tree | **done** |
| gameplay-tick authority transfer (the tick's state of record is the object graph) | **done** |
| whole-runtime DOS-layout detachment | **INCOMPLETE** |
| P5 memoryless runtime | **INCOMPLETE** |

Still true of the shipped build, and all of it disqualifies any "memoryless" claim:

- cold boot still **constructs a DGROUP byte image** (`boot_data.build_boot_memory()`);
- transitions and level loading still **enter image mode** (`enter_image_mode` — they mix view + raw `.data`);
- rendering still **consumes a materialised historical image** every frame;
- `NativeGameState` still owns a large `bytearray` (`.data`, 1 MB);
- `play_native --snapshot` still **loads a raw historical memory dump** (`memory_1mb.bin`).

Accurate claim today: *the RE workbench and emulator are gone from the shipped tree, and the gameplay tick runs
on the object graph.* NOT: *the product no longer speaks byte image* — it very much does.

### Replay/snapshot classification (audited 2026-07-16)

Classify by responsibility, not filename. `pre2/native/game_tick_demo.py` audits as **historical end-to-end** —
there is currently **no image-independent native replay to preserve**:

| capability | evidence | class |
|---|---|---|
| `gameplay_digest(dgroup)` | SHA1 of the 64K DGROUP | historical oracle |
| `GameTickDemo.seed` | `bytes(mem.data)` — a raw 1 MB image | historical |
| `GameTickDemo.digests` | per-tick DGROUP digests | historical oracle |
| `record_from_vm(rt, …)` | records FROM the VM via `mem.data[DS_BASE+o]` | historical / bridge |
| `verify_native(demo, …)` | native DGROUP digest vs the recording | historical oracle |
| `GameTickDemo.keys` | `bytes(mem.data[DS_BASE+o] for o in KBD)` | historical **encoding** of native input |
| `_inject(state, keys, idle)` | `state.wb(o,v)` **and** `state.data[DS_BASE+o] = v` | historical injection |

So the whole module belongs in bridge/dev tooling, and a **native** input replay (an event stream applied to the
native `Input` model) is *new work*, not a rescue. A future native save state must be its own format —
`NativeSaveState` (game model, object identities/refs, level/runtime state, scheduler, renderer-visible native
state, audio) — and must never be a disguised DGROUP dump. Keep the terminology and file formats for
**historical oracle snapshot** vs **native save state** distinct and never silently interchangeable.

### P5 acceptance wall

Do not define success by a deny-list entry. Define it by **physical impossibility**:

- `NativeGameState.data` (or any equivalent historical-memory authority) is **gone** from the release runtime;
- cold boot does not construct a DGROUP image;
- level loading and transitions do not enter image mode;
- the renderer does not materialise or consume the historical image;
- the release runtime imports no bridge, no historical layout serialiser, no DOS snapshot loader;
- **the game starts and plays with all historical-image modules physically unavailable**;
- the optional bridge can still serialise the native model into an oracle-comparable historical state, and the
  canonical deterministic demo stays byte-exact **through that external projection**.

Release-runtime dependency direction: `native model → gameplay → renderer/platform adapters`.
Optional verification direction: `native model → generated historical projection → oracle comparison`.
Forbidden: `native runtime → bridge`; `renderer → historical DGROUP image`; `transition code → image mode`;
`native save state → raw DOS memory dump`.

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
