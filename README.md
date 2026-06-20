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
- PRE2 reaches real graphics and the original title/menu screen from the original executable and `.sqz` assets.  The intro/title path can use linear VGA mode 13h, while map/level screens use the VGA/EGA-compatible 320x200 16-colour planar path.

## Run

Inspect the original files:

```bash
python scripts/play.py --inventory
```

Run the VM for a bounded number of instructions:

```bash
python scripts/play.py --steps 1000000 --trace-tail 40 --fast-adlib
```

Open the live graphics viewer with sound-card (OPL3) audio.  PRE2 runs in pure
original ASM here; the viewer renders BIOS text, linear VGA, and planar VGA/EGA
screens and plays the original AdLib register stream.  Inside it, `F12` saves a snapshot and `F11`
toggles input-demo recording:

```bash
python scripts/play.py --view
```

The viewer runs **unbounded** by default (it does not stop until you close the
window).  What to expect on a cold start:

- it boots through the LZEXE unpacker and lands on the **date/oldies text
  screen**, which waits for a key **press-and-hold** — hold `Enter` (or `Space`)
  to advance to the Titus/title VGA screens;
- `--speed N` sets the target VM steps/sec, i.e. the game **and music tempo**
  (default `120000`).  Lower it (`--speed 60000`) if the music is too fast; raise
  it to reach the game faster;
- `--fast-adlib` reaches the graphics fastest but **mutes the music** (it skips
  the interpreted AdLib driver).

Save a snapshot for later investigation:

```bash
python scripts/play.py --steps 1000000 --save-snapshot
```

Record and replay an input demo (deterministic; the substrate for regression
testing recovered code against the original):

```bash
python scripts/play.py --view --record-demo menu_nav          # record from launch (F11 toggles)
python scripts/play.py --play-demo artifacts/demo_menu_nav_<ts>          # replay headless
python scripts/play.py --play-demo artifacts/demo_menu_nav_<ts> --view   # watch the replay
```

Render a saved graphics snapshot to PNG if needed; the tool chooses linear VGA
or planar decoding from the snapshot metadata:

```bash
python scripts/render_frame.py artifacts/snapshot_pre2_YYYYMMDD_HHMMSS --out frame.png
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
