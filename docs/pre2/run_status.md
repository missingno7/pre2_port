# Prehistorik 2 run status

> The dated entries below `2026-06-20` are historical bring-up notes. Where they
> describe the project as pre-gameplay, list SQZ decompression as the "next
> blocker", or say `play.py --view` runs pure ASM, they are **superseded** by the
> entry below.

## 2026-06-20 recovery phase — gameplay runs, hybrid runtime, SQZ island done

The bootstrap phase is over. The VM **runs PRE2 gameplay correctly**, and recovered
native code is now part of the normal runtime.

- **Hybrid runtime is the default.** `create_pre2_runtime()` installs native
  **replacement** hooks (`pre2/replacements.py`) that run in place of the original
  ASM. `play.py --view` is the hybrid runtime, not pure ASM. The earlier
  VM-correctness fixes (BIOS ROM write-protect, ADC/SBB + shift/rotate flags, CRTC
  display-start reset, EGA read-mode-1 colour compare) made gameplay render correctly.
- **First recovered-native island: SQZ asset decompression** (`pre2/codecs/sqz.py`).
  All three formats — **LZSS** (`b4 4c` graphics, incl. >64KB outputs and the
  `byte-9==01` variant), **LZW**, and the **Huffman+RLE "other"** format — are
  recovered and verified byte-for-byte vs the ASM. The hybrid runtime cold-boots
  into gameplay decoding every asset natively (the old "SQZ loader/decompressor
  blocker" is resolved). This island merges into the future asset loader.
- **Three explicit modes, no silent fallbacks.** oracle/original
  (`native_replacements=False`), hybrid (default), verify (`--verify-hooks`,
  lockstep contract diff vs ASM). Unrecovered behaviour in hybrid mode fails loud
  (`Pre2HybridGap`).
- **Second recovered-native island: sprite-sheet decode** (`1030:42F7` local +
  `1030:436A` shared) — the first **stateful** island and the first memory-view ↔
  dataclass bridge (`pre2/bridge/sprites.py` ↔ `pre2/recovered/sprite_decode.py`).
  The level-load demux of the decompressed sprite sheet into the planar VRAM cache
  (`0xA000:0x5E80`) is recovered and **verified byte-for-byte vs the ASM** (a
  load-time witness, since the mid-game snapshot's sheet RAM is freed and its cache
  over-drawn). It runs **live in the hybrid runtime** (both adapters fire, hybrid
  cache byte-exact across 211 slots) with verify-mode lockstep coverage. The
  per-frame blit/scroll and the sprite classifier (`4213`, an EGA read-plane
  question) remain to recover.
- **Third recovered-native island: the sprite blit + classifier** (`1030:3B69`
  dispatcher + plain/empty/masked paths + `3D65` bg-restore, and the classifier
  `4213`). The classifier reads the cache in EGA read-mode-1 (colour compare) to
  build the per-sprite transparency type + masks; the blit renders each sprite
  from the planar cache by class — opaque copy, background restore, or masked
  composite `screen=(screen AND mask) OR sprite`. Recovered to a pure planar
  `renderer` module and **verified byte-for-byte vs the ASM** (per-blit witness +
  in-VM lockstep: 1002 blits across all paths, 0 divergence). Runs **live in the
  hybrid runtime** — hybrid renders level 1 correctly with ~950 native blits/frame.
  The island stops at the blit primitive; the tilemap/sprite-list **draw loops**
  (`34A0`/`3552`) that iterate game state are the next island.
- **Next:** the tilemap / object draw loops + background scroll/compose (the frame
  draw), then the object/player update. See [`recovery_architecture.md`](recovery_architecture.md)
  and [`symbol_ledger.md`](symbol_ledger.md).
- **Known gap (deferred):** gameplay audio is silent. The intro/title music is
  **AdLib FM** and plays (`0x388/0x389` → vendored `nuked_opl3`); but gameplay
  (mode `0x0D`) uses PRE2's **SoundBlaster digital path** (MOD music + PCM SFX via
  DSP/DMA), which the VM does **not** emulate yet — no SB DSP, no 8237 DMA, no SB
  IRQ. Recovering it is a game-independent `dos_re` subsystem (SB DSP + DMA channel
  + IRQ + PCM mix + `BLASTER` env). Deferred to after the sprite/tile pass.

## 2026-06-19 VGA boot milestone

The original `assets/pre2.exe` now cold-starts through the DOS_RE VM far enough to show real Prehistorik 2 graphics from the original executable and assets.

What works now:

- LZEXE 0.91 bootstrap reaches the inner PRE2 program code.
- The early PRE2 presentation can be skipped with the game's own INT 09h keyboard ISR.
- BIOS RTC calls used by the oldies/date screen are implemented: `INT 1Ah AH=02h` and `AH=04h`.
- The 386 CPU-probe path no longer dies on operand-size prefix `66h`; the VM executes the following instruction in the visible 16-bit low-word model.
- PRE2's VGA compatibility probe now passes:
  - VGA DAC write/read ports `03C7h`, `03C8h`, `03C9h`;
  - DAC pixel mask read `03C6h`;
  - EGA/VGA planar map-mask, read-plane, latch, data-rotate, and logical-op behaviour needed by the probe.
- DOS multiplex `INT 2Fh AX=4300h` reports XMS absent instead of killing the boot.
- The VM implements `CMPSB/CMPSW`, including `REP` semantics, which PRE2 uses after the Titus presentation.
- `scripts/play.py --view` now has a simple live VGA/text/EGA presenter and delivers keyboard scan codes through PRE2's own ISR.

Proof artifacts produced during bring-up:

```bash
artifacts/pre2_vga_boot_proof.png          # oldies/date screen after VGA/EGA probe
artifacts/pre2_mode13_present_loading.png  # Titus Presents in VGA mode 13h
artifacts/after_sprites_04.png             # PREHISTORIK 2 title/menu screen
```

Useful commands:

```bash
python scripts/play.py --inventory
python scripts/play.py --steps 1000000 --trace-tail 40 --fast-adlib
python scripts/play.py --view --fast-adlib --steps 50000000
python scripts/render_frame.py artifacts/after_sprites_04 --video vga --out title.png
```

Interactive notes:

- `--fast-adlib` is an opt-in PRE2 bootstrap accelerator that mutes/skips a hot AdLib tracker service thunk. It is not gameplay logic.
- The oldies/date screen uses press-and-hold semantics. Pressing and immediately releasing a key before the loop observes it does not skip it. In the live viewer, normal physical key hold/release goes through INT 09h.
- The title/menu screen is reached from original code and original `.sqz` assets; this is no longer a fake viewer or asset-only renderer.

## Current limitation

This is not yet a complete playable source port. The VM can launch to the PRE2 title/menu and proceeds through original asset loading, but there is no clean frame boundary, source-level gameplay model, or verified lifted game-loop yet. The next cut should be timer/frame/input pacing and stable snapshots around menu selection and level load.

## 2026-06-19 continuation: menu/input and SQZ-loader boundary

The current fork is now past the first "can it boot?" milestone and into the first real source-port boundary: menu/input and compressed asset loading.

New VM/runtime fixes from this pass:

- `INT 67h` EMS probes now report EMS unavailable instead of killing the run.
- 80186-compatible `PUSH imm16` / `PUSH imm8` are implemented; PRE2 can use these in inner paths even though the VM is still primarily a 16-bit oracle.
- Shift/rotate group-2 opcodes with immediate counts (`C0`/`C1`) are implemented.
- Rotate instructions no longer corrupt `ZF`/`SF`/`PF`; PRE2's SQZ bitstreams rely on carry flowing through `SHR`/`RCL` chains.
- Segment overrides now apply to string-operation source operands (`MOVS`, `CMPS`, `LODS`, `OUTS`). This is target-neutral and belongs in `dos_re`, not in `pre2`.
- The live viewer now uses `KeyDispatcher`, so fast physical taps are held across at least one emulated boundary instead of being pressed and released between two original polling points.
- `scripts/render_frame.py --video auto` chooses VGA mode 13h or EGA planar rendering from snapshot metadata and uses the saved VGA DAC palette / EGA CRTC display start.

Useful proof/diagnostic commands:

```bash
python scripts/render_frame.py artifacts/after_sprites_04 --video auto --out title.png
python scripts/render_frame.py artifacts/test_after_enter --video auto --out mode_beginner.png
```

Current deeper blocker:

- Starting the level reaches PRE2's real `.SQZ` loader/decompressor and opens `LEVEL1.SQZ`, `UNION.SQZ`, and `BACK0.SQZ`.
- The next divergence is inside/after the SQZ streaming decoder. It eventually falls into invalid/error-path code bytes such as `D8`/`D9`, but that is almost certainly a symptom, not a request to emulate x87 FPU for gameplay.
- The next productive cut is to isolate the SQZ decoder as an oracle routine: capture its input file stream, output segments, registers/flags, and stop condition, then lift just that codec under verification before touching gameplay.

## 2026-06-19 cleanup: graphics viewer, OPL3 audio, run/snapshot/demo harness

This pass made the runner match PRE2's actual hardware profile and gave us the
observation harness the source-port work needs before any hooks exist.

- **PRE2 graphics + text only.** CGA/Tandy target renderers were removed from
  `scripts/play.py`, `scripts/sdl_view.py`, and `scripts/render_frame.py`. PRE2
  uses BIOS text, linear VGA, and a VGA/EGA-compatible 320x200 16-colour planar
  path for game screens; the live viewer renders those states and no legacy
  target-specific modes.
  `render_frame.py` dropped its `--video`/`--palette` flags and now chooses the
  correct linear/planar decoder from snapshot metadata:
  `python scripts/render_frame.py <snapshot> --out frame.png`.
- **Sound-card audio, no PC speaker.** The dead `PcSpeakerAudio` path and the
  unused threaded OVERKILL viewer (`run_sdl_ui`) were deleted. The viewer now
  drives the vendored Nuked-OPL3 backend from the VM's forwarded AdLib/YM3812
  register stream (`dos.set_adlib_callback`).  `--audio adlib` (default) / `off`.
- **Run in pure ASM.** `play.py --view` runs the original executable with only
  the LZEXE bootstrap accelerator (and optional `--fast-adlib`) installed — no
  gameplay hooks — and is structured so hooks can be added later.
- **Snapshots + demos.** `F12` saves a snapshot; `F11` toggles input-demo
  recording (or `--record-demo NAME`). `--play-demo DIR` replays a demo
  (headless by default, `--view` to watch). A demo is a start snapshot plus
  VM-visible input keyed to a deterministic per-frame clock (fixed `chunk_steps`
  per frame); `chunk_steps`/`timer_irq`/`fast_adlib` are stored in the manifest
  and reapplied on replay. Verified: two headless replays of the same demo
  produce byte-identical memory and identical CPU state.

This is the harness for capturing the SQZ-decoder oracle: drive to the level
load, record a demo, snapshot before/after, then lift and verify the codec.

## 2026-06-19 fix: planar write-mode 1 for map/level screens

The `demo_pre2_20260619_213102` recording exposed a real VM graphics bug after
the main menu: the player sprite kept plausible colours, but the map/level
background and old menu sprites appeared shifted and mostly monochrome.  The root
cause was incomplete EGA/VGA planar write semantics.  PRE2 programs Graphics
Controller register 05h to write mode 1 during VRAM-to-VRAM copies; in that mode
the CPU byte is ignored and the previously-read VGA latch bytes are copied to the
destination planes.  The VM was incorrectly writing the CPU byte as normal data.

Fixes in this pass:

- `Memory` now tracks `ega_write_mode` and implements write mode 1 latch copies.
- `DOSMachine` tracks GC index 05h writes through ports 03CEh/03CFh.
- Snapshots save and restore `ega_write_mode`.
- `render_frame.py` again decodes planar snapshots using the saved CRTC display
  start, so evidence captures match the live viewer path.
- Regression tests cover write-mode 1 latch copies and map-mask behaviour.
