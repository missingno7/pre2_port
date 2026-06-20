# Recovery architecture & verification posture

This is the north star for the Prehistorik 2 recovery. It complements the
framework charter (`dos_re/AI_PORTING_CHARTER.md`) and methodology
(`docs/dos_re/source_port_methodology.md`); where this document and those agree,
that agreement is intentional.

## Goal

A faithful **recovered source port** — clean, readable high-level code that
resembles the original (almost certainly C) source the game was built from. Not
a loose remake, not an approximate editor-runtime. The original binary / ASM /
VM execution is the **oracle**, used to prove the recovered code behaves
identically. Over time the recovered high-level code becomes the real
architecture; the VM is retained only as a regression oracle.

## Hooks are probes, not logic

A hook is a **thin contact point** between the original ASM/VM world and our
recovered high-level world. It must not contain game logic. A hook only:

1. reads the relevant original memory/register state,
2. translates it into recovered structs/dataclasses,
3. calls clean high-level recovered functions,
4. compares/checkpoints against original ASM behaviour when asked,
5. writes results back only when it is replacing that ASM path,
6. returns to original control flow.

If logic is accumulating inside a hook, it belongs in a recovered function
outside the VM layer instead.

## Structs are reconstructed original layouts

Our structs/dataclasses are **not arbitrary modern abstractions**. They are our
reconstruction of the original C structs and memory layouts (PlayerState,
ObjectState, LevelState, CameraState, RendererState, asset records, …). The
clean functions should likewise read like recovered original functions. A hook
is the translation layer between raw VM memory and these structs.

## Islands grow and merge upward

Early on the project is many small verified **islands** connected to the ASM by
thin hooks: SQZ decode, LZW decode, sprite decode, masked blit, tilemap draw,
collision query, object/player update fragments. This is expected — but the
island shape is scaffolding, not the destination. Each island is written as real
recovered source from the start, and is designed so neighbouring islands can
later merge into larger subsystems, and eventually into a single high-level
`update_frame()`.

Verification/checkpoint boundaries move **up** as confidence grows, and become
fewer:

- **Early:** individual ASM routine boundaries — byte buffers, memory diffs,
  framebuffer diffs, small state contracts.
- **Middle:** subsystem boundaries — recovered PlayerState / ObjectState /
  LevelState / CameraState / RendererState.
- **Later:** whole frame/tick boundaries — run a full high-level update and
  compare the resulting semantic game state against the VM.

## Two modes

### Hybrid (normal play) — the active runtime

Default. Recovered native code runs **directly, in place of** the original ASM,
without constantly verifying against it. This keeps the game fast and playable,
and lets us record demos and snapshots. The hybrid path is the real runtime.

**No silent fallbacks.** If the hybrid runtime reaches something not yet
implemented or not understood, it must **fail loud** (a precise error / state
dump) rather than secretly running the original ASM and hiding the gap. A silent
fallback hides missing recovery work; fail-fast turns it into the next concrete
task. Consequence: hybrid playability is bounded by recovery coverage and grows
as islands are completed — that is expected and honest.

### Verify (separate, demo/snapshot-driven) — a debugging & proof tool

Strict, deterministic, divergence-focused. **Not** the normal architecture and
not a permanent lockstep straitjacket. Driven by recorded demos or snapshots:
replay the same inputs through the original ASM (oracle) and the recovered path,
compare at the current checkpoint boundaries (around islands early, higher
subsystem/frame boundaries later), and report the **first divergence** with
enough state to identify which recovered subsystem drifted.

Workflow: play in hybrid → record demos → if something looks wrong or a subsystem
needs validation, replay the demo in verify mode → diagnose the first divergence.

## Current state (slice 1: asset decompression)

`pre2/replacements.py` hosts the SQZ decompressor hook at `1030:1068`.

- Recovered & verified: **LZSS** (`b4 4c` graphics) and **LZW** (keyb/castle/
  present/titus) — `pre2/codecs/sqz.py`.
- **Gap (fails loud):** the "other" **Huffman+RLE** format used by `sample.sqz`
  and `theend.sqz` (`1030:10E6` decode, tree walker at `1030:11BD`). Hybrid stops
  loudly at `SAMPLE.SQZ` until this island is completed — by design.
- Verification (`--verify-hooks`): contract-level diff (output bytes + bump
  allocator `[1A13:2871]` + `ax`) at the decompressor's own RET sites; scratch
  registers are caller-dead and excluded. The strict full-state `HookVerifier`
  is reserved for finer hooks.
