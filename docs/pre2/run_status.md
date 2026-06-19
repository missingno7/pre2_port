# Prehistorik 2 run status

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
