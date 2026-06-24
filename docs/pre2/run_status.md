# Prehistorik 2 run status

## ★ CURRENT STATUS (2026-06-24) — authoritative; everything under the ARCHIVE divider is historical

**Phase:** hybrid recovered-source runtime + renderer recovery complete for gameplay/scenes;
**next phase = state ownership**.

- **Runtime:** `play.py --view` is the **HYBRID runtime by default** — recovered native replacements run
  in place of the ASM. The original ASM runs ONLY in `--no-replacements` (oracle) or `--verify-hooks`
  (verify) modes. Unrecovered behaviour fails loud (`Pre2HybridGap`), never a silent ASM fallback.
- **Rendering — recovered + live-grounded:** SQZ asset decode; sprite/object decode + classify + blit;
  the object-list draw pass (`26FA`); frame renderer (tile-row / grid / scroll / page-flip); HUD; iris;
  fireflies / particles / foreground-tile z-order; digital audio mixer + tracker. The faithful renderer
  (`--video faithful`) composes these recovered leaves into a clean framebuffer — it **never reads the VM VRAM**.
- **Scenes — grounded hook-first:** game-over (`9C87`), tally (`51A3`), OLDIES (`0C3E`), menu/map scroll
  (`scroll_blit` / `scroll_shift`), text (`draw_string`), and the title/intro 13h image (recovered +
  faithful path wired).
- **Remaining faithful-renderer gaps:** the two 0Dh scrolling-scene **compositions** (mode-select menu,
  map/carte) — **blocked on a history-dependent buffer** (recover the initial full-page-fill producer + a
  persistent-page model; do NOT rebuild from scratch). See `renderer_bug_table.md` #3/#5.
- **Still ASM — the next phase (state ownership):** gameplay UPDATE — movement / physics / collision / AI
  and the object-list state machine that drives the recovered renderer.

Canonical rules: `AGENTS.md` (north star + status taxonomy + collapse rule). Per-island status:
`recovered_islands.md`. Renderer detail: `faithful_visual_layer.md`.

---

# ═══════ ARCHIVED LOG (dated; most recent first) — SUPERSEDED by the CURRENT STATUS above ═══════

> These are dated work-log entries kept for provenance. Anything here that describes the project as
> pre-gameplay, lists SQZ as the "next blocker", says `--view` runs pure ASM, or states a "current
> limitation" is **superseded** — trust the CURRENT STATUS section above, not the entries below.

## 2026-06-24 — scene leaves grounded as live replacements (the recovered-leaf-first correction)

Architectural correction (now also in `AGENTS.md`): a recovered *scene* leaf that maps
to an original ASM draw routine must run **live in the hybrid runtime** (replace the ASM),
not only serve as a faithful-renderer mirror. Two per-frame scene drawers were grounded:

- **Game-over background scroll** — `pre2/checkpoints/gameover_scroll.py` replaces
  `1030:9C87` (the per-frame diorama windowed scroll-copy) with the recovered
  `window_scroll_copy`; EGA exit state write mode 1 / map mask 0x0F (453B). Verify Δ=0
  (8 entries); live trace fires 450×.
- **Tally panel** — `pre2/checkpoints/tally_panel.py` replaces `1030:51A3` (the per-frame
  "SCORE" / "LEVEL COMPLETED %" panel driver) with the recovered `render_tally_panel`; EGA
  exit state write mode 0 / map mask 0x08 (measured at a real 51DE ret). Verify Δ=0
  (6 entries); live trace fires 398×.

Each now follows the full shape: recovered leaf → live replacement hook → verify checkpoint
→ FaithfulVisual composes the same leaf. Proofs: `pre2/probes/verify_{gameover_scroll,
tally_panel}_hook.py`. One-shot image/copy paths (title/menu/oldies) deliberately stay
faithful-compose-only — small coastline shrink, the SQZ decode is already a live replacement.
Suite 281 passed.

## 2026-06-20 recovery phase — gameplay runs, hybrid runtime, SQZ island done

The bootstrap phase is over. The VM **runs PRE2 gameplay correctly**, and recovered
native code is now part of the normal runtime.

- **Hybrid runtime is the default.** `create_pre2_runtime()` installs native
  **replacement** hooks (`pre2/checkpoints/`) that run in place of the original
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
- **Second recovered-native island: sprite-sheet decode** (`1030:4316` local +
  `1030:4389` shared) — the first **stateful** island and the first memory-view ↔
  dataclass bridge (`pre2/bridge/sprites.py` ↔ `pre2/recovered/sprite_decode.py`).
  The level-load demux of the decompressed sprite sheet into the planar VRAM cache
  (`0xA000:0x5E80`) is recovered and **verified byte-for-byte vs the ASM** (a
  load-time witness, since the mid-game snapshot's sheet RAM is freed and its cache
  over-drawn). It runs **live in the hybrid runtime** (both adapters fire, hybrid
  cache byte-exact across 211 slots) with verify-mode lockstep coverage. The
  per-frame blit/scroll and the sprite classifier (`4232`, an EGA read-plane
  question) remain to recover.
- **Third recovered-native island: the sprite blit** (`1030:3B88` dispatcher +
  plain/empty/masked paths + `3D65` bg-restore). The blit renders each sprite from
  the planar cache by class — opaque copy, background restore, or masked composite
  `screen=(screen AND mask) OR sprite`. Recovered to a pure planar `renderer` module
  and **verified byte-for-byte vs the ASM** (per-blit witness + in-VM lockstep: 1002
  blits across all paths, 0 divergence). Runs **live in the hybrid runtime**.
  **The classifier `4232` is NOT recovered** — it still runs as ASM and produces the
  per-sprite type table (`[0x4DF4]`) + transparency masks the recovered blit
  *consumes*; recovering it (an EGA read-mode-1 colour-compare over the cache) is a
  pending island.
- **Fourth island — the frame renderer (the background draw), recovered + verified +
  live.** `pre2/recovered/frame_renderer.py` recovers the tile-row fill (`348D`), the
  full visible-grid redraw (`35A1`), the vertical scroll-copy (`3A27`), and the
  double-buffer page-flip (`3054`) — each composing the verified blit *directly*
  (recovered → recovered, no ASM contact point inside the draw), driven by the
  `Camera`/`ScrollState`/`TileMap` bridge (`pre2/bridge/frame.py`). All four are
  verified byte-for-byte vs a pure-ASM oracle (in-VM lockstep) and run live (8 native
  replacements). The compositor `3B40` is a static composition of these three and is
  documented but not wired (no available demo reaches it, so it can't be verified).
  **Still ASM:** the moving-sprite / object-list draw loop (`~3552`), the classifier
  `4232`, the directional-scroll camera logic, and all gameplay update.
- **Self-describing islands.** Each recovered function carries `@oracle_link(boundary,
  contract, status, merge_target)` (`pre2/islands.py`); the registry is auto-discovered
  into the generated [`recovered_islands.md`](recovered_islands.md) (regen:
  `python scripts/gen_island_manifest.py`) with a drift-check test — docs cannot
  diverge from code. The frame **adapters** (`pre2/checkpoints/frame.py`) are thin:
  they read VM state through the bridge dataclasses, call the recovered function, and
  write the contract back — no raw segment:offsets and no renderer logic live there.
- **Real-time pacing (live `--view`):** the VM now models **PIT channel 0** (the
  program programs ch0 reload `0x4000` → 72.83 Hz itself; we read it, never
  hardcode) and advances the **70 Hz VGA-retrace** bit on the wall clock, so PRE2's
  own timer/vsync waits set the speed. Live play self-paces to its native **~21.8
  Hz** with no `--speed` knob (the game's `1030:1C52` governor waits 3 ticks/frame).
  Record/replay keep the deterministic fixed-chunk clock (demos stay byte-exact).
  Snapshots now persist the PIT ch0 state. See `dos_re/dos.py` (`pit_channel0_*`,
  `_vga_status`) and `scripts/play.py`.
- **Gameplay audio now works (SoundBlaster emulated).** PRE2's digital audio (MOD
  music + PCM SFX) plays. The VM now models a generic **SB DSP + 8237 DMA channel +
  8259 PIC** (`dos_re/sblaster.py`, `dos_re/pic.py`), so the game's own driver
  auto-detects the card (base/IRQ/DMA — no `BLASTER` env needed) and streams 8-bit
  unsigned PCM, which the viewer resamples and mixes with the OPL stream
  (`scripts/sdl_view.py`). Enabled in the live viewer via `runtime.enable_sound_blaster`
  (off for deterministic demos/tests). Two BIOS-correctness fixes were needed: the
  BIOS data area CRTC port (`0040:0063`) and `IRET` stubs on the hardware-IRQ
  vectors. A residual audio-pacing nicety (occasional buffer pressure) remains.
  **Layering (be precise):** this is *generic SB/DMA/PIC hardware in `dos_re`* + the
  *original PRE2 audio driver running as ASM*. It is **NOT recovered PRE2 audio
  source** — the game's software mixer (`1030:218F` per-channel + SFX, the DMA-refill
  ISR `20AB`) and the audio asset models are still ASM. "Audio works through the
  emulated SB + original driver" ≠ "audio decode/mixer recovered." `dos_re` holds no
  PRE2-specific audio/asset knowledge.
- **Bug fixed (also fixed a graphics-corruption regression):** the SQZ **LZSS bump
  advance** used `(reserved>>4)+1`, but the ASM (`1030:1450`) pre-shifts the size's
  high byte twice — so we over-reserved ~4× for assets with a non-zero high byte
  (sprites/union/menu/.trk), eventually pushing decodes into VRAM. Now
  `sqz_bump_advance()` matches the ASM exactly.
- **VM video oracle fix — planar mode-set now clears the shadow planes.** A BIOS
  Set-Video-Mode (`INT 10h AH=00`, AL bit7 clear) clears display memory; for the
  planar EGA modes (`0Dh`…) PRE2's pixels live in the four shadow planes
  (`EGA_APERTURE = 0x100000`), but the VM was zeroing only the legacy `0A000h`
  aperture — a **no-op for planar pixels**, so the previous screen survived a mode
  transition (the menu→map scrolled the old mode-select image in instead of black).
  `dos._clear_graphics_vram_for_mode` now clears all four shadow planes for planar
  modes. Generic VM/BIOS correctness (no PRE2-specific clear); regression-tested
  (`tests/test_core.py::test_mode_set_clears_planar_shadow_planes`, and the bit7
  "no clear" case). Confirmed by a cold-boot trace (the `0Dh` mode-set that left
  stale planes now zeroes them).
- **Next:** the moving-sprite / object-list **draw** path (`~3552`) — treated as a
  renderer island first (object draw-command stream → recovered `blit_sprite`), paired
  with a `pre2/bridge/objects.py` `ObjectDrawState`; the classifier `4232` it depends
  on; then object/player **update** (movement/physics/collision). See
  [`recovery_architecture.md`](recovery_architecture.md) and
  [`symbol_ledger.md`](symbol_ledger.md); recovered-island truth is
  [`recovered_islands.md`](recovered_islands.md).

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

## (historical, 2026-06-19) Limitation as of that date — SUPERSEDED

> Superseded: a clean frame boundary (`6772`), the recovered gameplay frame renderer, and the verified
> hybrid runtime now exist (see CURRENT STATUS). Kept for provenance only.

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
