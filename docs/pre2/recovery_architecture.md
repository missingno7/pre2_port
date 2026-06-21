# Recovery architecture & verification posture

This is the north star for the Prehistorik 2 recovery. It complements the
framework charter (`dos_re/AI_PORTING_CHARTER.md`) and methodology
(`docs/dos_re/source_port_methodology.md`); where this document and those agree,
that agreement is intentional.

## Goal — and the shape we're crystallizing toward

A faithful **recovered source port**: clean, readable high-level code that
resembles the original (almost certainly C) source the game was built from. Not a
loose remake, not an approximate editor-runtime. The original binary / ASM / VM
execution is the **oracle**, used to prove the recovered code behaves identically.

The strongest single principle:

> We do **not** want the final project shaped by hundreds of low-level hooks. We
> want it shaped by **reconstructed structs, recovered functions, and high-level
> systems**. Hooks and checkpoints are temporary contact points; dataclasses and
> recovered functions are the source port crystallizing out of the original game.

Move strongly toward clean high-level recovered code — without ever losing the
ability to verify each subsystem against the original.

## Hooks are scaffolding — every hook has a role and a lifetime

A hook is a **thin contact point** between the original ASM/VM world and our
recovered world. It must not contain game logic. A hook only:

1. reads the relevant original memory/register state,
2. translates it into recovered structs/dataclasses (via memory views),
3. calls clean high-level recovered functions,
4. compares/checkpoints against original ASM behaviour when asked,
5. writes results back only when it is replacing that ASM path,
6. returns to original control flow.

Every hook/checkpoint must declare **which of four roles** it plays — and that
role is its intended *lifetime*, not a permanent fixture:

- **probe** — observe the original ASM (tracing, capturing oracles);
- **verifier** — checkpoint a recovered island against the original;
- **replacement adapter** — replace a known ASM path in the hybrid runtime;
- **gap detector** — expose an unrecovered/not-understood path by failing loud.

A hook must never silently drift into permanent gameplay structure. If logic is
accumulating inside one, it belongs in a recovered function outside the VM layer.

## Every island declares what it merges into

Early on the project is many small verified **islands** connected to the ASM by
thin hooks. This is expected — but the island shape is scaffolding, not the
destination. Each island is written as real recovered source *from the start*,
and must declare the larger system it will merge upward into:

| Island (now) | Merges into (later) |
|---|---|
| codec (SQZ/LZW/…) | asset loader |
| masked blit | renderer |
| collision query | physics / collision system |
| object update fragment | object system |
| player update fragment | player update |
| frame fragment | full `update_frame()` |

Neighbouring islands coalesce into subsystems, and subsystems into a single
high-level `update_frame()`. Verification boundaries rise with them (next section).

## Structs/dataclasses are the bidirectional bridge

Our dataclasses are **not arbitrary modern abstractions** — they are our
reconstruction of the original C structs and memory layouts (`PlayerState`,
`ObjectSlot`, `LevelState`, `CameraState`, `RendererState`, `GameState`, asset
records, …). They are the **main translation layer** between original memory and
recovered source, and they must connect in **both directions**:

- **original ASM memory → dataclass** — readable from the live VM through a
  *memory view* (the byte layout / field offsets the game actually uses);
- **recovered logic → original memory** — the same dataclass is consumed and
  produced by clean recovered functions (`update_player()`, `update_object()`,
  `collision_query()`, `update_frame()`), and can be written back into VM memory
  when that ASM path is being replaced.

The bridge, end to end:

```
original ASM memory
  → memory views
    → recovered structs/dataclasses
      → clean recovered functions
        → semantic state comparison
          → (optional) write-back to original memory when replacing ASM
```

This is what lets the project move fast toward readable high-level code without
losing verifiability — the dataclasses are simultaneously the recovered source's
data model *and* the verification surface.

## Verification compares contracts, not accidental ASM shape

We want **exact behaviour**, but not permanent dependence on every tiny accidental
ASM boundary. Verification compares *contracts*, and the contract level rises as
understanding improves:

- **Early:** raw memory diffs, register/flag diffs, output-buffer diffs,
  framebuffer/pixel diffs — at individual ASM routine boundaries.
- **Later:** **semantic state contracts** — `PlayerState` / `ObjectState` /
  `LevelState` / `CameraState` / `RendererState` / `GameState`, and whole
  frame/tick boundary comparisons.

The long-term verification model is therefore **state-level**, not address-level:

1. read original machine memory into recovered dataclasses (via memory views),
2. run recovered high-level logic on those dataclasses,
3. compare the resulting dataclass/state contract against the original VM state,
4. drop to raw memory/register diffs **only** when diagnosing a lower-level
   divergence.

So per-hook-address diffing is an early scaffold; as islands merge, checkpoints
become fewer and move up to clean semantic boundaries (asset load, renderer
output, collision query, player/object update, frame/tick, major game-state).

## Two runtime modes

### Hybrid (normal play) — the active runtime

Default. Recovered native code runs **directly, in place of** the original ASM,
without constantly verifying against it. This keeps the game fast and playable,
and lets us record demos and snapshots. The hybrid path is the real runtime.

**No silent fallbacks.** If the hybrid runtime reaches something not yet
implemented or not understood, it must **fail loud** (a precise error / state
dump) rather than secretly running the original ASM and hiding the gap (a "gap
detector" hook). A silent fallback hides missing recovery work; fail-fast turns
it into the next concrete task. Consequence: hybrid playability is bounded by
recovery coverage and grows as islands are completed — that is expected and honest.

### Verify (separate, demo/snapshot-driven) — a debugging & proof tool

Strict, deterministic, divergence-focused. **Not** the normal architecture and
not a permanent lockstep straitjacket. Driven by recorded demos or snapshots:
replay the same inputs through the original ASM (oracle) and the recovered path,
compare at the current contract boundaries (around islands early, higher
subsystem/frame/state boundaries later), and report the **first divergence** with
enough state to identify which recovered subsystem drifted.

Workflow: play in hybrid → record demos → if something looks wrong or a subsystem
needs validation, replay the demo in verify mode → diagnose the first divergence.

## Slice 1 — asset decompression: complete

The first recovered island. `pre2/replacements.py` hosts the SQZ decompressor
hook at `1030:107B` (role: *replacement adapter*, with a *verifier* at the
decompressor RET sites; this island merges into the **asset loader**).

- **All three SQZ codecs recovered and verified byte-for-byte vs the ASM**
  (`pre2/codecs/sqz.py`): **LZSS** (`b4 4c` graphics, incl. >64KB outputs and the
  `byte-9==01` wrapper variant — sprites is byte-exact), **LZW**
  (keyb/castle/present/titus), and the **Huffman+RLE "other"** format
  (sample/theend). The hybrid runtime cold-boots into gameplay decoding every
  asset natively, with zero gaps.
- **Contract = output bytes + bump allocator `[1A0F:2875]` + `ax`.** The bump
  advances by the header's *reservation* field, not the decode length (LZSS
  over-reserves) — `sqz_reserved_size()`. Caller-dead scratch registers are
  excluded.
- **Verification** (`--verify-hooks`): contract-level diff at the decompressor's
  own RET sites (`15EF`/`1328`/`11F0`). The strict full-state `HookVerifier` is
  reserved for finer hooks.
- A cautionary note kept in the project memory: a hand-rolled "size == header"
  invariant once *falsely* condemned the (correct) LZSS decoder. Heuristic
  contracts can be wrong; the authoritative verifier is the lockstep against the
  ASM, not a guessed invariant.

Next island: the first **stateful** subsystem (sprite/tile decode → renderer),
where the first memory-view ↔ dataclass bridge is stood up.
