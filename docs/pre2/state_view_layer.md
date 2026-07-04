# The state-view layer — how recovered logic reaches game state

> Companion to [`recovery_architecture.md`](recovery_architecture.md). That doc sets the
> philosophy ("one recovered implementation, many adapters; hybrid is the workshop, faithful
> is the product"). This one documents the **concrete seam** that realizes it: how a recovered
> function reads and writes game state without knowing whether it is driving the live VM, a
> `NativeGameState`, or a verification overlay — and where the original DOS memory offsets live.

## The problem it solves

The original game is a 16-bit DOS binary; its state is a 64 KB data segment (DGROUP) full of
fixed-offset variables, fixed-stride arrays, and in-memory pointers. Early recovered code spoke
that layout directly — `rw(0x6BF6)`, `ov.ww(di + 4, ...)`, `mem.data[DS + off]`. That works and
verifies, but it **couples the logic to the DOS memory image**: the *what* (advance the wind,
project the sprite) is buried under the *where* (which byte). It is unreadable, and it makes the
recovered layer look like a transliteration instead of source.

We want the recovered logic to read like source — `s.wind`, `slot.x`, `entry.threshold` — with the
byte offsets confined to one small, swappable layer. Critically, we must do this **without weakening
byte-exact verification**, which today is a trivial `memcmp` precisely *because* the native state
**is** the DOS memory image.

## The shape: one view API, swappable backends (ports & adapters)

```
        recovered logic  (pure — the WHAT)
        s.wind   slot.x = ...   entry.threshold
                    │
                    │  human-named fields, no offsets
                    ▼
        view        (ScrollScriptView, RenderSlot, SwarmView, …)   ── the WHERE (layout)
        StructView / StructArray / _U8 / _U16 / _S16 / _U16Array
                    │
                    │  field → backend.rb/rw/wb/ww(offset)
                    ▼
        backend     (one of three — the HOW)
        ├── ByteBackend          → the 1 MB image      (native runtime + memcmp verify)
        ├── OverlayBackend       → {off: val} contract (read-through; contract islands)
        └── WidthContractBackend → {off:(val,width)}   (write-only projection passes)
```

The recovered function is written **once**, against the view API. Which backend is behind the view
decides what "reading state" *means* at that moment — live image, or an accumulating write-contract.
The logic does not know or care. This is exactly the *"one implementation, many adapters"* the
architecture doc calls for, made concrete.

Everything lives in **`pre2/bridge/dgroup_view.py`** — the *only* file that writes down a DGROUP
offset for a migrated island. That is the "layout bridge": pure Python (no `cpu`/`mem`/`dos_re`),
importable by both recovered logic and the VM adapters.

## The three backends, and when each applies

An island's backend is dictated by **how that island returns its result** — which is fixed by its
byte-exact golden, so you match the golden, you don't choose freely.

| Backend | Reads | Writes | Contract format | Used by |
|---|---|---|---|---|
| `ByteBackend` | the 1 MB image | mutate the image | *(none — the image itself)* | native runtime; **verification** (memcmp) |
| `OverlayBackend` | read-through (sees own writes) | accumulate | `{offset: value}` (byte) | whole-routine transforms that return a write set over a read-through overlay (terrain `_Ov`) |
| `WidthContractBackend` | delegate to the island's `rb`/`rw` | accumulate | `{offset: (value, width)}` | write-only projection passes (effect/particle projection) |

That there are three (not one) is a real property of the codebase: **islands use different contract
conventions.** The view layer adapts to each rather than forcing a rewrite of every golden. A future
cleanup could unify the conventions and collapse backends; it is not required.

## The vocabulary (descriptors & views)

Field descriptors resolve `view._base + their offset` against the view's backend:

- `_U8(off)` / `_U16(off)` — unsigned byte / little-endian word.
- `_S8(off)` / `_S16(off)` — signed variants.
- `_U16Array(off, len)` — `view.field[i]` word array.
- `StructArray(off, stride, len, cls)` — `view.slots[i]` → a `StructView` bound to `base + i*stride`.

View bases:

- `StructView(backend, base)` — one struct at a DGROUP-offset base; its fields are *relative*.
- `DgroupView(source)` — whole-DGROUP view (base 0, so fields are absolute DGROUP offsets); wraps a
  `NativeGameState` / VM `mem` / raw `bytearray` in a `ByteBackend`, or takes a backend directly.

Because `StructView` and `DgroupView` share descriptors, the **same** `_U16(4)` serves both a top-level
variable and a field inside an array element. Special cursors (e.g. `_ScriptEntry`) snapshot their base
so they keep reading the original record after the parent advances a pointer — matching the ASM.

## Shared layouts

A game struct that several islands touch is named **once** and reused. The flagship is `RenderSlot`
(stride 0x12): the on-screen entity record — `x`, `y`, `sprite` (packed id+flags), `flags`, `source`,
`life`, plus a `sprite_id` convenience. It is the projection target of terrain-entities, the
effect-particle projector, and the field source of the main sprite renderer — one view, all three.
These slots are ~40 % of all DGROUP offsets, so naming them is the single biggest readability payoff.

## The property that makes it safe: verification never changes

The byte-backed view writes **straight through** `state.data`. So after any migration:

- the island's **existing golden test passes with the same hashes** (contract unchanged), and
- the **forward oracle** (native vs VM, memcmp of the DGROUP image) is byte-for-byte identical.

We never serialize a separate representation to verify. Verification always runs on the byte backend,
so the "clean" representation and the "verifiable" representation are the *same bytes*. This is why the
migration can proceed island-by-island with **no window where correctness is unprovable**.

## Where this sits in the layer stack

```
pre2/codecs/     asset decoders (SQZ, sprite) — pure
pre2/recovered/  the game's behaviour — pure source-level logic (the WHAT)   ← reads via the view API
pre2/bridge/     layout + VM⇄dataclass translation (the WHERE + oracle glue) ← dgroup_view.py lives here
pre2/native/     the VM-less runtime: NativeGameState + the frame driver     ← constructs views, calls recovered fns
dos_re/          the emulator (oracle only) — game-agnostic
scripts/         play_native (product), play (hybrid/verify workshop)
```

Import direction: `native → bridge → recovered → codecs`; `dgroup_view.py` is a leaf (imports nothing
of ours), so both recovered logic and the VM adapters may use it without a cycle.

## The plan, in four points

1. **The native runtime does not need to stop being byte-backed to be a real VM-less Python game.**
   Byte-backed ≠ VM-backed: a `bytearray` + an offset map is pure Python data — no CPU emulation, no
   segment translation, no `dos_re`. The release already ships no VM, no EXE, no boot image.
2. **The milestone that matters is that gameplay logic stops knowing raw offsets** — that is the whole
   cleanliness win, and it is independent of the storage representation underneath.
3. **The byte-backed adapter is a legitimate release citizen, because it is none of the three things the
   architecture forbids** — it is not the DOS EXE, not a VM, and not a silent ASM fallback. It is just the
   internal state representation that preserves faithful behavior *and* verification (the two are literally
   the same bytes, which is why verification stays a `memcmp`). Its shippability rests on `dgroup_view.py`
   staying **pure** (no `dos_re`/probe/checkpoint imports) — which it is.
4. **Optional enhancements are not enabled by the state-view layer directly.** They belong to the semantic
   render-intent / enhanced-renderer boundary (below).

## Function of the "bridge" — and what is optional

"Bridge" (`pre2/bridge/`) is everything that connects the pure recovered core to a concrete world:

1. **Layout** — `dgroup_view.py`: the human-name ↔ DGROUP-offset binding + the backends. Used at
   runtime *and* for verification.
2. **VM⇄dataclass readers** — e.g. reading the sprite list / camera / palette out of live VM (or native)
   memory into the recovered dataclasses the renderer consumes.
3. **Oracle glue** — snapshots, demos, the verify harness: load a DOS/VM state, replay recorded input,
   compare the recovered path against the original ASM.

**What is genuinely optional at release:** items (2)-(3)'s *oracle* half — the VM, snapshot/demo replay,
and verification machinery are dev-time only and are already stripped from the native deployment
(`deploy_native.py`; no `dos_re`, no EXE, no boot image ship).

**What is *not* (yet) optional:** the **layout** (item 1). Native runs on the byte-backed view, which
*is* the offset map, so the offset table is linked in the shipped game. Making it physically removable
would require a **field-backed backend** (plain Python attributes, no offsets) behind the same view API —
which in turn needs a lossless DGROUP↔fields serializer for verification. **This is deliberately not
pursued:** for a faithful port whose state genuinely *is* a memory image, the byte backend already
delivers clean logic *and* exact verification, and a few KB of always-linked layout is a non-issue. The
field-backed backend remains a clean future option (the seam is designed for it) but is not a goal.

## What this does and does not buy — and the enhancement foundation

This layer's job is a **clean, readable, faithful native core**: gameplay logic that reads like source,
with the DOS layout quarantined and verification intact. That is its whole remit.

It is **not** the mechanism for optional *enhancements*. Smooth interpolation, modern scaling, and
low-latency audio attach at a *different* seam — the semantic **render-intent model** the faithful
renderer emits (see [`render_model.md`](render_model.md), [`enhanced_renderer_design.md`](enhanced_renderer_design.md)),
and the enhanced-is-presentation-only rule (`recovery_architecture.md`). Keep the two separate:

- **State-view layer (this doc)** → clean *simulation* code. Enables nothing visual by itself.
- **Render-model seam** → clean *presentation* boundary. This is what optional enhancements build on.

A tidy simulation core makes the whole thing easier to reason about and extend, but an enhancement that
needs data the faithful core doesn't yet expose must be recovered at the source layer first — **never
faked in the renderer.**

## Migration recipe & status

Per island: pass a view (or a backend + view) in → name the **stable** fields → leave genuinely
*union-typed* offsets (an offset read at different widths per entity type) as **raw backend access with a
comment** (inventing three aliases for one triple-typed offset adds more noise than it removes) → verify
against the island's existing golden / the forward oracle.

Proven end-to-end (byte-exact) so far: the scripted-scroll/LEVELG-snow island (scalars, word-array, packed
rng, pointer-chased script table); the firefly swarm (array-of-structs); terrain-entities (overlay backend
+ `RenderSlot` projection); effect-particle projection (`WidthContractBackend` + `RenderSlot`); the main
sprite renderer (`RenderSlot` on the hottest path — measured ~2.4× per-read but < 1 % of frame budget, so
negligible). **No infrastructure gaps remain** — the rest is a mechanical, always-verifiable sweep.
