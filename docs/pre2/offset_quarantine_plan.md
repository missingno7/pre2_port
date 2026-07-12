# Plan: get the original-memory layout out of the shipped product

Goal (user, 2026-07-13): the native release code should be fully human-readable — **no `rb`/`rw`/`wb`/`ww`,
no raw DGROUP offsets, no `DATA_SEG`/`<<4`** in shipped layers. Everything that ties the code to the original
game's memory image moves into the detachable component. A **linter** enforces the ban so it can't regress.

This doc plans what that actually means, what is reachable, what is *not* (and why), and the phased path.

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

## 2. Two possible end-states — pick one

**E1 — "names, not numbers" (recommended, achievable).**
Shipped code expresses gameplay purely as named state + named tables + record handles. The offset *numbers*
and the byte-translation machinery live in / behind the detachable layer. A tiny, clearly-labeled
**quarantine module** holds the irreducible pointer residue (§4). The byte image survives as the state
substrate (it is the game's heap), which keeps verification a `memcmp`.

**E2 — "no memory image at all" (not recommended).**
Reimplement the game's linked structures as real Python object graphs (lists of dataclasses, no arena).
This **breaks byte-exact verification** (the image is no longer the state — every digest needs a
reconstruction step), contradicts the v0.4.0 finding that *the image is the heap*, and invites behavioral
drift in exactly the subtle pointer-following code that is hardest to test. It is a rewrite, not a cleanup.

The rest of this plan assumes **E1**.

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

---

## 4. The irreducible residue — be honest about it

Some recovered code follows pointers the **game itself stores in its own memory**:

- **Stored absolute record pointers** (~13 sites): `di = rw(TARGET_A)` — `[0xA423]` holds an *offset* into
  DGROUP; the code dereferences it (`slot = RenderSlot(be, di)`). The field access can be named (and is), but
  `di` is a raw offset the original algorithm computed.
- **Variable-stride arenas**: the 2nd-pass entity list (`0x8489`) is walked by `si += [si]` — the stride is
  read *from* the data. There is no fixed-index array to name; it is a linked structure.
- **Live script cursors**: the camera/boss script interpreters advance an offset cursor through bytecode
  stored in game state.

You cannot express these on a pure name-keyed store without reimplementing the data structures (→ E2, which
we rejected). Under E1 they get **quarantined**: one small module (working name `pre2/native/raw_arena.py`)
that is the *only* shipped place allowed to do offset arithmetic, is loudly documented as "the byte-exact
pointer core — intrinsic to fidelity, not a cleanup miss," and is the single entry on the linter's allowlist.
Everything else in the product is offset-free. Estimated residue: **< 30 sites**, down from ~787.

---

## 5. Phased path (each phase gated by the full proof corpus)

**Phase 0 — the linter (build first, so every later phase ratchets).**
`scripts/lint.py` gains a shipped-layer rule: no `rb(`/`rw(`/`wb(`/`ww(` calls, no `state.data[` indexing,
no `<< 4` / `DATA_SEG` / `DGROUP_BASE`, no numeric-literal first arg to a view/record constructor. Allowlist
exactly the quarantine module (§4) and the translation machinery *until* it relocates. The linter starts
**advisory** (reports a shrinking count) and flips to **enforcing** per file as each is cleaned — a ratchet,
never a big-bang.

**Phase 1 — finish the record-interior conversions** (continue task #18). Kills kind-2 `rb`/`rw`. Files:
object_tick, player_collision, the remaining object_spawn/combat/player_interaction sites; assess terrain
(polymorphic) and object_inject (arena) honestly rather than forcing dishonest names.

**Phase 2 — read-only tables → named `TableView`s.** A new view kind: `floor_props[tile]`, `half_height[id]`,
`hurt_sfx[n]`, the level-map reader. Base offsets move to the layout map; shipped code indexes by meaning.

**Phase 3 — relocate the translation machinery.** Consolidate `readers`/`apply_ds`/`tile_reader`/`DATA_SEG`
into one adapter, then push it behind the detachable boundary; the native loop asks for named views, not
readers. Removes kind-4 from `views/`.

**Phase 4 — the FieldBackend product flip.** Run the tick core on the name-keyed store for scalars + fixed
records; keep a byte image only for the arena/pointer residue (a hybrid store). Proven by the existing
`verify_field_flip.py` per-tick round-trip + the digest corpus.

**Phase 5 — quarantine the residue** (§4) and make the linter fully enforcing. Document the module as the
intrinsic byte-exact core.

---

## 6. Honest cost & risk

- **Scale**: ~787 shipped raw-access sites + ~40 table bases. This is weeks of careful, byte-exact batches,
  not a single pass. Every batch must stay green on 916 pytest + lint + the 5-demo tick corpus
  (919/207/187/68/15) + deploy smoke — the same discipline that carried the field-backed campaign.
- **Risk**: the read-through vs read-original backend semantics (a converted read must see the same memory
  the ASM saw) is the recurring footgun — caught only by the corpus, which is why every batch runs it.
- **The payoff is real but bounded**: the product becomes ~97% offset-free and reads as source; a small,
  honestly-labeled pointer core remains because byte-exact fidelity to a pointer-based 1993 DOS heap
  *requires* it. That residue is a feature of faithfulness, not a debt.
