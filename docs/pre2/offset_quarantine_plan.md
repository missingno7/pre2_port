# Plan: the object-model product — the original memory becomes a detachable verification layer

Goal (user, 2026-07-13): the native release runs on a pure Python **object graph** (dataclasses) — **no
`rb`/`rw`/`wb`/`ww`, no raw DGROUP offsets, no `DATA_SEG`/`<<4`, no byte image**. The *entire* original-memory
world (layout, offsets, byte translation) moves into a **detachable bridge** used only for verification.
Detached, the end user gets a clean native port that (by design) **cannot** play demos or load snapshots;
attached, the bridge serializes the object graph to a bit-exact DGROUP image and proves the port matches the
original ASM tick-for-tick. A **linter** enforces the ban so it can't regress.

This doc plans what that means, why it is reachable, where the difficulty concentrates, and the phased path.
It supersedes an earlier "quarantine the residue" framing (see §2) — the object model is cleaner.

---

## 1. What "connection to original memory" actually is — the taxonomy

Grep says the shipped layers hold **~787 `rb`/`rw`/`wb`/`ww` sites** (recovered 550, native 237, views 82,
enhanced 11) plus ~9,900 hex literals. But those are not one problem — they are five, with very different
difficulty:

| # | kind | example | count (rough) | fate |
|---|---|---|---|---|
| 1 | **Named scalar state** | `rb(0x27D6)` = energy | most of native/views | → field view (`g.energy`) — largely done |
| 2 | **Fixed-stride records** | `writes[(si+4)] = 0x1CB` | ~120 | → record view (`slot.sprite`) — task #18, in progress |
| 3 | **Read-only tables** | `rb(0x7F5E + tile)` floor props | ~40 bases | → named `TableView` (`floor_props[tile]`) — not started |
| 4 | **Translation machinery** | `readers()`, `apply_ds()`, `DATA_SEG`, `<<4` | ~10 defs, 546 refs | → move to the bridge; inject named views |
| 5 | **Irreducible pointer residue** | `di = rw(TARGET_A)`; variable-stride arena walk | ~13 stored ptrs + 2 arenas | → **quarantine** (see §4) |

The critical honesty is the split between **1–4 (eliminable)** and **5 (not fully eliminable)**.

### Why "magic hex" is the wrong thing to ban

~5,800 of the recovered hex literals are `0xFFFF`, `0xFF`, `0x80`, `0x8000`, `0x12` (a stride), sprite ids
(`0x2046`), velocity caps, timings. Those are **game logic**, not memory layout — a faithful port keeps them
(optionally named for meaning, but they are not "the connection to the original"). Deciding "is this hex an
offset?" is undecidable in general.

**The linter insight: don't ban the numbers, ban the verbs.** Forbid `rb(`/`rw(`/`wb(`/`ww(`, `state.data[`,
`<< 4`, `DATA_SEG`/`DGROUP_BASE`. Once those verbs are gone from shipped code, a stray offset literal has
nothing to be *passed to* — it becomes inert — and everything left is provably arithmetic. This makes the
rule decidable and mechanical.

---

## 2. The chosen end-state — the object model (decided 2026-07-13)

The target is the **holy grail**: the shipped product runs on a pure Python **object graph** (dataclasses) —
no `bytearray`, no offsets, no `rb`/`rw` — and the entire original-memory world (the layout, the offset
numbers, the byte translation) lives in a **detachable bridge** used only for verification. Detached, the
product is a clean native port; attached, the bridge can serialize the object graph back to a bit-exact
DGROUP image and prove the port matches the original ASM tick-for-tick.

Two user decisions pin the design (2026-07-13):

- **Detached = no replay/snapshot at all.** Demos and snapshots are byte-level artifacts; they are a
  bridge/verification concern only. The shipped product carries zero replay code. (Cleanest boundary.)
- **Verification stays byte-level: serialize → `memcmp`.** The bridge serializes the object graph to a
  bit-exact image and compares vs the VM every tick — the strongest guarantee, catching even bytes we did
  not model. This requires the bit-exact serializer (§4).

**Why this supersedes the earlier "quarantine" framing.** An earlier draft called the pointer-following code
(`di = rw(TARGET_A)`, the variable-stride arena, script cursors) an *irreducible residue* that had to stay in
the product as a quarantined offset-speaking module. That is only true *while the product keeps the byte
image*. With a real object model the residue **dissolves** — a stored offset becomes an object reference, an
arena becomes a `list`, a script cursor becomes an index. Nothing in the product speaks offsets; the offset
knowledge **relocates entirely into the bridge serializer**, which is exactly the goal. So the object model is
architecturally *cleaner* than quarantining, not a compromise.

**Why it is now feasible.** The hardest precondition — knowing *every* mutable byte of state — is already met:
the v0.4.0 field-backed campaign proved WRITTEN-UNNAMED = 0 over the corpus. A lossless object model needs
exactly that census, and it exists.

**The rejected alternative** was keeping the byte image as the product's state substrate (verification-as-
`memcmp`-for-free, no serializer to build). Cleaner to *build*, but it never reaches the holy grail — the
product forever carries the image and the offset residue. We are trading a one-time serializer effort for a
permanently offset-free product.

---

## 3. What "move the offsets to the bridge" concretely requires

Two things live in shipped code today that must relocate:

**(a) The translation machinery.** `readers()`, `tile_reader()`, `apply_ds()`, `DATA_SEG`, and the
`<<4 + offset` address math currently sit in `pre2/views/*.py` — a *shipped* layer. These are pure
layout/translation with no gameplay decisions. They move to the detachable side; shipped code receives
already-bound named views and never constructs a reader or touches `state.data`.

**(b) The offset authority.** `dgroup_view.py`'s descriptors (`energy = _U8(0x27D6)`) are today both the
human-readable *structure* (names, widths, which array) **and** the *physical* offset table. To get numbers
out of shipped code we split those roles:

- the **structure** (field names, records, arrays — the readable declaration) stays shipped;
- the **physical offsets** become a generated layout map used *only* to (de)serialize a DGROUP image —
  which is a bridge concern (snapshots / digests / verification).

This only pays off if the **product runtime stops needing offsets at all**, i.e. runs on the name-keyed
`FieldBackend` (already built, §4 of the field-backed campaign) rather than a byte image. Then offsets are
*runtime-dead* — needed only when the bridge is attached to read/write a real image. That is the flip that
makes the offset table genuinely detachable rather than just "moved to another shipped file."

**(c) Read-only game content.** The tilemap, the ~hundreds of sprite-definition records, the animation
scripts, and the property tables (floor `0x7F5E`, ceiling `0x7E5E`, half-widths/heights `0x7190/0x7191`,
slope `0x8E1D`, cos/sin `0x6F90/0x7090`, …) are *content*, not mutable state — but gameplay reads them by
offset today. For the object model these become **loaded content objects** the level loader produces
(`level.tilemap`, `level.sprite_defs[id]`, `floor_props[tile]`). This is the **long pole** of the effort: the
mutable state is ~4,900 already-named fields, but the read-only content is larger and currently offset-addressed
throughout. It migrates at the loader (`level_load` decodes SQZ assets into structured content instead of into
image bytes).

---

## 4. The pointer code — dissolved by the object model, concentrated in the serializer

Some recovered code follows pointers the **game itself stores in its own memory**. Under a byte image these
are irreducible offset arithmetic. Under the **object model** each becomes an ordinary object relationship,
and the offset reconstruction moves to the bridge serializer:

| in the byte world | in the object model | the serializer's job |
|---|---|---|
| `di = rw(TARGET_A)`; `[0xA423]` holds an offset | `camera.target: RenderRecord \| None` (a reference) | write the referent's assigned offset into `0xA423` |
| variable-stride arena `0x8489`, `si += [si]` | `entities: list[Entity]` (each carries its kind) | pack the list back in order, re-emit each stride byte |
| a script cursor offset into bytecode | an index into a named `list[Opcode]` | index → the cursor's absolute offset |

So there is **no quarantine module** — nothing in the product speaks offsets. Instead there is one new
bridge artifact, the **bit-exact serializer** `object_graph → DGROUP image`, and it is where all the offset
complexity concentrates (which is the goal). Its hard parts:

1. **Pointer reconstruction** — assign every object a stable offset (fixed-slot arrays: index → offset;
   the arena: packing order), then write each reference field as its referent's offset.
2. **Arena packing** — reproduce the 2nd-pass entity list's exact record order and per-record strides.
3. **Freed-slot stale bytes** — the DOS game leaves junk in freed slots; the model must preserve it, so slot
   arrays stay **fixed-length with a `dead` flag** (never dynamic lists that drop entries), or the `memcmp`
   diverges in regions the digest masks do not cover.
4. **Constant fill** — the ~45k untouched / read-only bytes come from a per-level **template image**; the
   serializer overlays the object graph's mutable bytes on top. (The field-backed census guarantees the
   overlay covers every mutable byte.)

The serializer becomes a trusted artifact, and the tick corpus is exactly what earns that trust: run
native-on-objects and the VM from the same seed, `serialize(world)` each tick, `memcmp` vs the VM.

---

## 5. Phased path (each phase gated by the full proof corpus)

The object model is the **terminal state of the phases already in flight** — not a separate project. The
FieldBackend flip (Phase 4) is the pivot ("product runs without the image"); the object model is that plus
the read-only-content migration (§3c) plus modeling the pointer relationships (§4).

**Phase 1 — record-interior conversions** (task #18) — **essentially DONE.** Kind-2 `rb`/`rw` gone from the
clean fixed-stride files (object_spawn, effects_update, combat_interaction, player_interaction, object_tick).
terrain (polymorphic) and object_inject (arena) were assessed and left as honest arithmetic — they become
`list`/`Entity` objects at the model step, not forced names now.

**Phase 2 — read-only tables → named accessors.** `floor_props[tile]`, `half_height[id]`, `hurt_sfx[n]`, the
level-map reader. First step of the §3c content migration; base offsets move toward the bridge layout.

**Phase 3 — relocate the translation machinery.** Consolidate `readers`/`apply_ds`/`tile_reader`/`DATA_SEG`
into one adapter and push it behind the detachable boundary; the native loop asks for named views, not
readers. Removes kind-4 from the shipped `views/` layer.

**Phase 0 — the linter** (build *after* Phase 3, per the 2026-07-13 decision to shrink the surface first).
Shipped-layer rule: no `rb(`/`rw(`/`wb(`/`ww(`, no `state.data[`, no `<< 4` / `DATA_SEG` / `DGROUP_BASE`, no
numeric-literal first arg to a view/record constructor. Starts **advisory** (shrinking count), ratchets to
**enforcing** per file as each is cleaned.

**Phase 4 — the FieldBackend product flip.** Run the tick core on the name-keyed store; the byte image is no
longer the product's state. Proven by `verify_field_flip.py` per-tick round-trip + the digest corpus.

**Phase 5 — the object model + the serializer.** Wrap the name-keyed state as real dataclasses; build the
bit-exact serializer (§4) in the bridge; migrate the remaining read-only content (§3c). Detached, the product
is pure objects with **no replay/snapshot**; attached, the bridge serializes → `memcmp` verifies. The linter
goes fully enforcing with an **empty** allowlist — the holy grail.

---

## 6. Honest cost & risk

- **Scale**: ~787 shipped raw-access sites + ~40 table bases. This is weeks of careful, byte-exact batches,
  not a single pass. Every batch must stay green on 916 pytest + lint + the 5-demo tick corpus
  (919/207/187/68/15) + deploy smoke — the same discipline that carried the field-backed campaign.
- **Risk**: the read-through vs read-original backend semantics (a converted read must see the same memory
  the ASM saw) is the recurring footgun — caught only by the corpus, which is why every batch runs it.
- **The new artifact to trust**: the bit-exact serializer (§4). It concentrates all the offset/layout
  complexity in one bridge module — pointer reconstruction, arena packing, stale-byte preservation. It is
  earned by the same tick corpus that proves everything else.
- **The payoff**: a **fully** offset-free product — pure dataclasses, no image, no `rb`/`rw`, empty linter
  allowlist. The entire original-memory world lives in the detachable bridge; plug it in to verify, unplug it
  to ship. That is the holy grail, and the v0.4.0 "every mutable byte is named" result is what makes it
  reachable rather than aspirational.
