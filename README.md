# Prehistorik 2 DOS_RE Source-Port Workbench

This fork is now aimed at **Prehistorik 2**.  The reusable `dos_re` real-mode VM is kept as the execution oracle, while game-specific work lives under `pre2/`.

The immediate goal is not a clean remake yet.  The goal is to boot the original packed `PRE2.EXE`, observe the real runtime, save snapshots, and then start replacing understood routines with verified source-level code.

## Current state

- `assets/pre2.exe` is the canonical executable.
- The original Prehistorik 2 `.sqz` assets and `.trk` music files are kept under `assets/`.
- The old legacy game package, legacy target docs, and stale legacy target artifacts were removed.
- `dos_re.bootstrap_lzexe` now contains a target-neutral LZEXE 0.91 loop accelerator.
- `pre2.runtime.create_pre2_runtime()` launches `pre2.exe` through the generic DOS VM and installs only bootstrap helpers, not gameplay hooks.
- The VM now handles early PRE2 startup needs that were missing from the legacy target fork: BIOS `INT 10h AH=1Bh`, BIOS font function `INT 10h AH=11h`, `INT 1Ah` RTC calls, `INT 2Fh` XMS probing, `INS/OUTS`, `CMPSB/CMPSW`, and the small VGA/EGA register model needed by PRE2's compatibility checks.
- PRE2 reaches real VGA mode 13h graphics and the original title/menu screen from the original executable and `.sqz` assets.

## Run

Inspect the original files:

```bash
python scripts/play.py --inventory
```

Run the VM for a bounded number of instructions:

```bash
python scripts/play.py --steps 1000000 --trace-tail 40 --fast-adlib
```

Open the live bring-up viewer:

```bash
python scripts/play.py --view --fast-adlib --steps 50000000
```

Save a snapshot for later investigation:

```bash
python scripts/play.py --steps 1000000 --save-snapshot
```

Render a saved video memory snapshot if needed:

```bash
python scripts/render_frame.py artifacts/snapshot_pre2_YYYYMMDD_HHMMSS --video vga --out frame.png
python scripts/render_frame.py artifacts/snapshot_pre2_YYYYMMDD_HHMMSS --video ega --out frame.png
```

Known proof snapshots from this milestone include `artifacts/pre2_mode13_present_loading` and `artifacts/after_sprites_04`.

## Architecture rule

`dos_re/` must stay game-independent.  Anything that knows Prehistorik 2 filenames, executable layout, bootstrap policy, or future gameplay addresses belongs under `pre2/`.

The intended migration path is:

```text
original PRE2.EXE
  -> dos_re VM
  -> bootstrap/source snapshots
  -> PRE2-specific views over original memory
  -> verified hooks
  -> semantic source-port systems
```

The packed executable and VM remain the oracle until a piece of behavior has been observed and verified.


Current status: PRE2 boots to the original title/menu in the VM, input is delivered through the original INT 09h path, and the next blocker is the real SQZ level-loader/decompressor boundary.
