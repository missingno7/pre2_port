# Prehistorik 2 DOS_RE Source-Port Workbench

A faithful **recovered source port** of **Prehistorik 2**, grown from the original
`PRE2.EXE` under a custom real-mode VM. The reusable `dos_re` VM is the execution
**oracle**; all game-specific recovery lives under `pre2/`. Recovered code moves
gradually upward into clean, VM-independent source-like modules — not a loose
remake, and not a permanent forest of low-level hooks.

## Current state — recovery phase

The bootstrap phase is done. The VM **runs PRE2 gameplay correctly**, and
recovered native code is now part of the normal runtime:

- **The hybrid runtime is the default.** `pre2.runtime.create_pre2_runtime()`
  installs native **replacement** hooks that run *in place of* the original ASM.
  As coverage grows the game runs faster and more of it is clean source.
- **The recovered asset + rendering pipeline is real, verified source.** Recovered
  byte-for-byte vs the ASM and running live: **SQZ asset decode** (all three formats —
  LZSS / LZW / Huffman+RLE; `pre2/codecs/sqz.py`), **sprite-sheet demux** into the
  planar cache (`pre2/recovered/sprite_decode.py`), the **sprite blit** primitive
  (`pre2/recovered/renderer.py`), and the **frame renderer** — tile-row fill, grid
  redraw, scroll-copy, page-flip (`pre2/recovered/frame_renderer.py`), each composing
  the verified blit directly and driven by `Camera`/`ScrollState`/`TileMap` views
  (`pre2/bridge/frame.py`). The authoritative, code-generated list of recovered
  islands (with status + ASM boundary + contract + merge target) is
  [`docs/pre2/recovered_islands.md`](docs/pre2/recovered_islands.md).
- **Still ASM (not yet recovered):** the moving-sprite / object-list draw loop, the
  sprite classifier (`4232`, which produces the type/mask data the recovered blit
  consumes), and all gameplay update (movement, physics, collision, objects/player).
- **No silent fallbacks.** If the hybrid runtime reaches behaviour we have not
  recovered, it **fails loud** with a precise gap report (`Pre2HybridGap`) instead
  of secretly running the original ASM. Running the original ASM is allowed, but
  only in an explicit, mode-controlled way (oracle / verify modes).

See [`docs/pre2/recovery_architecture.md`](docs/pre2/recovery_architecture.md) for
the north-star architecture and [`docs/pre2/run_status.md`](docs/pre2/run_status.md)
for the running log.

## Execution modes

| Mode | What runs | Use |
|---|---|---|
| **oracle / original** | pure original ASM (replacements removed) | reference & observation; capturing oracles |
| **hybrid (default)** | recovered native replacements run directly, no per-step verification | normal play, recording demos/snapshots |
| **verify** | ASM runs as oracle and each recovered result is diffed against it at contract boundaries | offline proof against recorded demos/snapshots |

- `play.py --view` → hybrid runtime (the active runtime).
- `play.py --view --verify-hooks` → verify mode (lockstep contract check vs ASM).
- `create_pre2_runtime(..., native_replacements=False)` → pure oracle/ASM mode.

## The game executable — bring your own legal copy

This is a reverse-engineering / source-port **workbench**, not a redistribution of
the game. It operates on the original `PRE2.EXE` and the Prehistorik 2 data files,
which are **not** included in this repository — you must supply them from a copy of
the game **you legally own**.

- This project targets the **GOG.com DRM-free release** of Prehistorik 2. Buy it
  there (or use another copy you legally own) and copy `PRE2.EXE` plus the game data
  files into [`assets/`](assets/).
- Addresses, offsets, and the recovered logic in this tree are all derived against
  that GOG `PRE2.EXE`; a different build will have a different memory layout and the
  hooks will not line up.
- Do not commit the game binary or assets. They stay local to your checkout.

## Run

```bash
python scripts/play.py --inventory                       # inspect original files
python scripts/play.py --view                            # live viewer, hybrid runtime + OPL3 audio
python scripts/play.py --view --verify-hooks             # play with the lockstep ASM oracle check
python scripts/play.py --steps 1000000 --save-snapshot   # headless snapshot for study
python scripts/render_frame.py artifacts/<snapshot> --out frame.png
```

In the viewer: `F10` saves a screenshot, `F11` toggles input-demo recording, `F12`
saves a VM snapshot. Live `--view` self-paces to PRE2's native tempo via the emulated
PIT/VGA-retrace (no tempo knob); `--speed N` only affects deterministic record/replay.
`--fast-adlib` reaches graphics fastest but mutes music.

Demos are the regression substrate — a start snapshot plus VM-visible input on a
deterministic per-frame clock, replayable headlessly and through the verify mode:

```bash
python scripts/play.py --view --record-demo run1
python scripts/play.py --play-demo artifacts/demo_run1_<ts> --verify-hooks   # prove no drift vs ASM
```

## Architecture in one breath

```text
original PRE2.EXE
  -> dos_re VM (oracle)
  -> thin replacement adapters / checkpoints (pre2/checkpoints/, one module per subsystem)
  -> memory views / dataclass bridge (pre2/bridge/: sprites, frame, ...)
  -> recovered VM-independent logic (pre2/codecs/, pre2/recovered/)
  -> semantic state comparison -> source-port systems
```

Each recovered function self-describes via `@oracle_link(...)` (`pre2/islands.py`),
auto-discovered into the generated [`docs/pre2/recovered_islands.md`](docs/pre2/recovered_islands.md)
(a test fails if it drifts from the code). Adapters under `pre2/checkpoints/` stay
thin — read VM state through the bridge, call the recovered function, write the
contract back; renderer/game logic and raw segment:offsets do not live there.

`dos_re/` must stay game-independent: anything that knows Prehistorik 2 filenames,
addresses, or formats belongs under `pre2/`. The packed executable and VM remain
the oracle until a piece of behaviour has been observed, recovered, and verified.
Methodology lives in [`AGENTS.md`](AGENTS.md), [`ARCHITECTURE.md`](ARCHITECTURE.md),
[`dos_re/AI_PORTING_CHARTER.md`](dos_re/AI_PORTING_CHARTER.md), and the
[`docs/`](docs/) tree; the original-address ledger is
[`docs/pre2/symbol_ledger.md`](docs/pre2/symbol_ledger.md).
