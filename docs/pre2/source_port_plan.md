# Prehistorik 2 source-port plan

## Non-negotiable boundary

`dos_re` is the reusable DOS machine.  It must not know Prehistorik 2 addresses, assets, command-line quirks, or source-port assumptions.

`pre2` is the game-specific layer.  It owns original PRE2 filenames, executable inventory, bootstrap helpers, future address maps, and later verified hooks.

## Phase status

**Bootstrap milestones — done.** Boot the packed `pre2.exe` through the VM; treat
LZEXE as bootstrap (target-neutral accelerator); collect stable snapshots; trace
`.sqz`/`.trk` loads; render the VGA/EGA screens. **The VM now runs gameplay.**

**Recovery phase — in progress.** Replace understood routines with verified
native code, running by default in the **hybrid** runtime, and move recovered code
upward into clean VM-independent modules. Each island: find the ASM/data boundary,
define the input/output contract, observe I/O, write clean native logic, verify
against the ASM, then wire a thin adapter — and only then trust it.

### Recovered islands

Each island declares the larger subsystem it will **merge into** (the coastline must move upward
over time — see `recovery_architecture.md`; hooks are scaffolding, not the final architecture).

The authoritative island list is **generated from the code** — each recovered function carries its own
`@oracle_link(boundary, contract, status, merge_target)` metadata (`pre2/islands.py`), auto-discovered into
[`recovered_islands.md`](recovered_islands.md) (regenerate: `python scripts/gen_island_manifest.py`; a test
fails on drift). The table below is the human-curated roadmap; the manifest is the source of truth for what
is recovered.

| Island | Module | Merge target | Status |
|---|---|---|---|
| SQZ decompression (LZSS/LZW/Huffman+RLE) | `pre2/codecs/sqz.py` + `pre2/checkpoints/sqz.py` | asset loader | **done, verified vs ASM** |
| sprite/tile decode | `pre2/recovered/sprite_decode.py` + `pre2/bridge/sprites.py` | sprite/asset pipeline | **done, verified vs ASM** (first stateful island; stood up `pre2/bridge/`) |
| sprite blit + background restore (`3B69`) | `pre2/recovered/renderer.py` + `pre2/checkpoints/blit.py` | renderer | **done, verified vs ASM** (in-VM lockstep) |
| SoundBlaster audio (DSP + 8237 DMA + 8259 PIC) | `dos_re/sblaster.py` + `dos_re/pic.py` (generic hw) | DOS machine (not game layer) | **done** — game auto-detects + DMA-streams PCM; user-confirmed |
| frame renderer — tile-row (346E), grid redraw (3582), scroll-copy (3A08), page-flip (3035) | `pre2/recovered/frame_renderer.py` + `pre2/bridge/frame.py` (Camera/ScrollState/TileMap) | frame renderer → `update_frame()` | **done, all four verified vs ASM** (in-VM lockstep, 0-div; each composes the verified blit). Compositor `3B40` is a static composition of these, documented but not wired (no demo reaches it → can't verify yet) |
| moving-sprite / object-list draw (`~3552`) | `pre2/bridge/objects.py` + `pre2/recovered/object_draw.py` (planned) | frame renderer | **next** — renderer island; command-stream verified; composes recovered `blit_sprite` |
| gameplay systems (player/object/level update) | `pre2/recovered/` (planned) | object system / player update / physics | later; semantic-state verification |

**Still ASM (the current coastline — not recovered):**
- **classifier `4213`** — the ASM producer of the sprite type table (`[0x4DF4]`) + partial-sprite masks
  (`[0x2DF4]`) that the recovered blit *consumes*. A pending island (needs a pure fn + `@oracle_link` +
  manifest entry + verify before it counts as recovered).
- **moving-sprite / object-list draw loop** (`~3552`) — the next island.
- **directional-scroll decisions** above the recovered leaves (which way/when to scroll; `3344`/`338E`/…).
- **level-load orchestration** (decides what to load and sets up the segment pointers; the per-asset codec is recovered).
- **all gameplay update** — movement, AI, collision, physics, object/player state.

## Audio recovery (layered)

The emulated SoundBlaster/DMA/PIC (`dos_re`) + the original ASM audio driver are the **oracle/bootstrap
path, not the final architecture**. The goal is a clean recovered **`AudioSystem`** so hybrid play needs
neither the ASM driver nor the emulated SB (which stay as oracle/verify backends). Recover in layers, with
verification rising from bytes → state → PCM:

1. **Asset decode** — **DONE** (`pre2/codecs/audio.py`, `tests/test_audio_assets.py`): `.TRK` = SQZ-LZSS
   standard ProTracker **M.K.** module (all 12 parse, layout closes exactly); `SAMPLE.SQZ` = SQZ-"other" →
   60768-byte 8-bit PCM SFX bank. (SQZ decode itself was already recovered.)
2. **Data model** — `SampleBank`/`Module`/`Pattern`/`Instrument`/`ChannelState`/… (`ModModule`/`ModSample`
   exist); raw layout → `pre2/bridge/audio.py`.
3. **Tracker/playback** — sequencer `1030:221A` → `pre2/recovered/tracker.py` (only effects PRE2 uses).
4. **Mixer** — per-channel `1030:216B` + SFX `20AB-20F3` + DMA-refill ISR `2029` → `pre2/recovered/mixer.py`;
   verify same state+SFX+timing → same PCM block vs `sb.pcm_out`.
5. **Integration** — detach hybrid play from the ASM audio path (recovered `AudioSystem.tick → mixer →
   backend`); ASM/SB stay oracle-only.

`dos_re` holds **no** PRE2-specific audio knowledge; tracker/mixer logic never lives in checkpoints/adapters.
"Audio works via emulated SB + original driver" ≠ "audio decode/mixer recovered."

## Recovery rules (kept short; full posture in `recovery_architecture.md`)

- Three explicit modes; the original ASM runs only in **oracle**/**verify** modes,
  never as a silent fallback. Hybrid mode fails loud on gaps (`Pre2HybridGap`).
- Recovered logic is clean, VM-independent (no `cpu`/`mem`/`dos_re`); hooks are
  thin adapters/verifiers with a declared role (probe / verifier / replacement /
  gap-detector), not where logic accumulates.
- Dataclasses reconstruct the original C-like structs; the bridge layer reads them
  from VM memory and (when replacing) writes them back. Verification rises from
  byte/buffer diffs to semantic state contracts over time.

## Reference

- Original addresses, continuation points, allocator state, and decode boundaries:
  [`symbol_ledger.md`](symbol_ledger.md).
- `pre2.exe` is LZEXE 0.91-packed MZ; the asset set is dozens of `.sqz` (recovered
  decompressor) and `.trk` music files.
