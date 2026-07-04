# Prehistorik 2 — recovered VM-less source port

A faithful, **VM-less** source port of **Prehistorik 2**, reconstructed from the original
`PRE2.EXE` one verified island at a time. The game now runs as clean, standalone Python: the
custom `dos_re` real-mode VM is kept **only as an offline oracle** for byte-exact verification —
it is not started when you play, and no `PRE2.EXE` or boot image is executed at runtime.

## Current state — the native game plays

`scripts/play_native.py` is the product: it **cold-boots the whole game with no emulator**, from
the initialized data segment (`pre2/native/boot_data.py`, no EXE needed) plus the GOG `*.SQZ`
assets, and drives the recovered flow end to end —

> OLDIES credits → TITUS / PREHISTORIK-2 title screens → menu → world-map (carte) → gameplay →
> level-end tally → next level → … → endings → game-over → restart.

Everything the frame touches is recovered source, verified byte-for-byte against the ASM:

- **Rendering** — SQZ asset decode (LZSS/LZW/Huffman+RLE), sprite/tile demux + classify, the sprite
  blit, the moving-sprite/object-list draw (`26FA`), the frame renderer (tile-row / grid redraw /
  scroll-copy / page-flip), HUD, level-end iris, fireflies / particles / foreground-tile z-order,
  parallax, and the LEVELG falling snow. A faithful renderer composes these leaves into the
  framebuffer — it never reads VM VRAM.
- **Gameplay** — the whole per-frame loop: player FSM + movement + collision, the object/entity
  state machine, the second pass, terrain/moving-platform entities, the effects passes, the combat
  pass (`88D7`), and the level state machine (death / respawn / checkpoint / level-end / game-over /
  game-complete).
- **Audio** — digital SFX playback and per-level ProTracker music, from the recovered banks.
- **Front-end** — intro, both title screens, the attract animation, the difficulty menu, password
  entry, the world-map/carte scroll-in, transitions and DAC fades.

Verification is byte-exact and continuous: a recorded demo is replayed through the VM oracle and
the native core **tick by tick**, and the full DGROUP image is compared — native reproduces the VM
exactly (render/async offsets excluded). See [`docs/pre2/recovery_lifecycle.md`](docs/pre2/recovery_lifecycle.md).

**Known residuals** (honest, small): a few rare edge-case paths still fail loud rather than run
(e.g. the game-over-via-respawn tail, a blocked cave-pan); the level-end tally shows the exact
score/percent but not yet the animated count-up *cutscene*; and the state-view cleanup (below) is
an in-progress sweep. None block a normal playthrough.

## Two runners: the product and the workbench

| | Runner | VM? | For |
|---|---|---|---|
| **Native** (product) | `scripts/play_native.py` | none | playing / shipping the recovered game |
| **Hybrid + verify** (workbench) | `scripts/play.py` | yes (oracle) | recovering & proving new behaviour against the ASM |

The workbench is where an island is prepared and proven; the **same** recovered functions then run
in the native core with the hooks gone. Hybrid never silently falls back to ASM — an unrecovered
path **fails loud** (`Pre2HybridGap`). See
[`docs/pre2/recovery_architecture.md`](docs/pre2/recovery_architecture.md).

## Run

```bash
# the native game — no emulator, cold boot from the first screen
python scripts/play_native.py                       # full boot: OLDIES → titles → menu → play
python scripts/play_native.py --from-level 0        # debug: drop straight into LEVEL1
python scripts/play_native.py --snapshot <dir>      # resume a saved gameplay state (VM-less)

# ship it — standalone folder + optional PyInstaller exe (no VM, no EXE)
python scripts/deploy_native.py                     # build + smoke-test dist/pre2native/
python scripts/deploy_native.py --exe               # + PyInstaller (needs `pip install pyinstaller`)

# the workbench — hybrid runtime + the ASM oracle
python scripts/play.py --view                       # live viewer, recovered hooks over the VM
python scripts/play.py --view --verify-hooks        # lockstep contract check vs the ASM
python scripts/play.py --view --record-demo run1    # record a regression demo
python scripts/play.py --play-demo artifacts/demo_run1_<ts> --verify-hooks   # prove no drift
```

Controls (native): `SPACE` = advance / fire+jump, arrows or numpad = move, `ESC` = quit.

## Bring your own legal copy

This is a source-port **workbench**, not a redistribution. It needs the original Prehistorik 2 data
files, which are **not** in this repository — supply them from a copy you legally own.

- Target the **GOG.com DRM-free release**. Copy the game's `*.SQZ` / `*.TRK` data into
  [`assets/`](assets/), or point `--game-root` at your GOG install folder.
- All addresses and recovered logic are derived against that GOG build; another build has a
  different memory layout and will not line up.
- The workbench (`play.py`, verification) also needs the GOG `PRE2.EXE` in `assets/`; the native
  game (`play_native.py`) does **not**.
- Never commit the game binary or data — they stay local to your checkout.

## Architecture in one breath

```text
game data (*.SQZ / *.TRK)  +  boot constants (pre2/native/boot_data.py)
  → recovered logic          (pre2/codecs/, pre2/recovered/)  — pure, VM-independent source
  → state-view + bridge      (pre2/bridge/)                   — human-named views ⇄ DGROUP layout
  → native runtime           (pre2/native/)                   — NativeGameState + the frame driver
  → faithful renderer + audio → the standalone game (scripts/play_native.py)

  dos_re/  — the real-mode VM, kept ONLY as the verification oracle (never shipped, never at runtime)
```

Gameplay logic reads **human-named fields** (`player.x`, `slot.sprite`), never raw offsets — the DOS
memory layout is quarantined in one view layer (`pre2/bridge/dgroup_view.py`); see
[`docs/pre2/state_view_layer.md`](docs/pre2/state_view_layer.md). Each recovered function
self-describes via `@oracle_link(...)` (`pre2/islands.py`), auto-discovered into the generated
[`docs/pre2/recovered_islands.md`](docs/pre2/recovered_islands.md) (a test fails if it drifts from
the code — the manifest is the source of truth for what is recovered).

`dos_re/` stays game-agnostic: anything that knows Prehistorik 2 filenames, addresses, or formats
lives under `pre2/`. Methodology and posture: [`docs/`](docs/),
[`docs/pre2/recovery_architecture.md`](docs/pre2/recovery_architecture.md),
[`AGENTS.md`](AGENTS.md), [`ARCHITECTURE.md`](ARCHITECTURE.md); the original-address ledger is
[`docs/pre2/symbol_ledger.md`](docs/pre2/symbol_ledger.md).
