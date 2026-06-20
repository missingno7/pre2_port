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

| Island | Module | Merge target | Status |
|---|---|---|---|
| SQZ decompression (LZSS/LZW/Huffman+RLE) | `pre2/codecs/sqz.py` + `pre2/replacements.py` | asset loader | **done, verified vs ASM** |
| sprite/tile decode | `pre2/recovered/sprite_decode.py` + `pre2/bridge/sprites.py` | sprite/asset pipeline | **done, verified vs ASM** (first stateful island; stood up `pre2/bridge/`) |
| sprite blit + background restore + type dispatch | `pre2/recovered/renderer.py` + `pre2/replacements.py` | renderer | **done, verified vs ASM** (in-VM lockstep, hybrid renders level 1) |
| SoundBlaster audio (DSP + 8237 DMA + 8259 PIC) | `dos_re/sblaster.py` + `dos_re/pic.py` (generic hw) | DOS machine (not game layer) | **done** — game auto-detects + DMA-streams PCM; user-confirmed |
| frame renderer — tile-row draw (346E) | `pre2/recovered/frame_renderer.py` + `pre2/bridge/frame.py` (Camera/ScrollState/TileMap) | frame renderer → `update_frame()` | **done, verified vs ASM** (in-VM lockstep, 33 row-draws 0-div; composes the verified blit). Scroll-copy/compositor (`3A08`/`3582`/`3B40`) + directional scroll remain (indirectly dispatched → semantic frame/tick checkpoint) |
| gameplay systems (player/object/level update) | `pre2/recovered/` (planned) | object system / player update / physics | later; semantic-state verification |

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
