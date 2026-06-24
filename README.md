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
- **Gameplay rendering is recovered, verified source running live.** Byte-for-byte vs
  the ASM and replacing it in the hybrid runtime: **SQZ asset decode** (LZSS / LZW /
  Huffman+RLE; `pre2/codecs/sqz.py`), **sprite-sheet demux**, **sprite/object classify**,
  the **sprite blit**, the **moving-sprite / object-list draw pass** (`26FA`), the
  **frame renderer** (tile-row / grid redraw / scroll-copy / page-flip), the **HUD**, the
  end-level **iris**, **fireflies / particles / foreground-tile z-order**, and the digital
  **audio mixer + tracker**. The faithful renderer (`--video faithful`) composes these SAME
  recovered leaves into a clean framebuffer — it never reads the VM VRAM.
- **Non-gameplay scenes are grounded hook-first too.** Live-grounded: **game-over** (`9C87`),
  **tally** (`51A3`), **OLDIES** glyph (`0C3E`), the menu/map **scroll** (`scroll_blit` /
  `scroll_shift`), **text** (`draw_string`); and the title/intro **13h image** is
  codec-decoded + composited (`render_title_image`, faithful path wired). The only remaining
  faithful-renderer gaps are the two **0Dh scrolling-scene compositions** (mode-select menu,
  map/carte), blocked on a history-dependent buffer (see
  [`docs/pre2/faithful_visual_layer.md`](docs/pre2/faithful_visual_layer.md)). The
  code-generated island list is
  [`docs/pre2/recovered_islands.md`](docs/pre2/recovered_islands.md).
- **Still ASM — the *state-ownership* track, not rendering.** Gameplay UPDATE (player/object
  movement, physics, collision, AI, and the object-list state machine that produces what the
  recovered renderer draws) is still interpreted ASM; recovering those controllers is the
  next phase. The rendering/audio output is recovered; the state that drives it is not yet.
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
