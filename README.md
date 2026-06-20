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
- **First recovered-native island: SQZ asset decompression** (`pre2/codecs/sqz.py`)
  — all three formats (LZSS, LZW, Huffman+RLE "other") recovered and verified
  byte-for-byte against the ASM. The decompressor was the slow hot kernel on the
  level-load path; the hybrid runtime now cold-boots into gameplay decoding every
  asset natively.
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

## Run

```bash
python scripts/play.py --inventory                       # inspect original files
python scripts/play.py --view                            # live viewer, hybrid runtime + OPL3 audio
python scripts/play.py --view --verify-hooks             # play with the lockstep ASM oracle check
python scripts/play.py --steps 1000000 --save-snapshot   # headless snapshot for study
python scripts/render_frame.py artifacts/<snapshot> --out frame.png
```

In the viewer: `F10` saves a screenshot, `F11` toggles input-demo recording, `F12`
saves a VM snapshot. `--speed N` (default `120000`) sets the game+music tempo;
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
  -> thin replacement adapters / checkpoints (pre2/replacements.py)
  -> recovered VM-independent logic (pre2/codecs/ ... ; future pre2/recovered/)
  -> memory views / dataclass bridge (future pre2/bridge/)
  -> semantic state comparison -> source-port systems
```

`dos_re/` must stay game-independent: anything that knows Prehistorik 2 filenames,
addresses, or formats belongs under `pre2/`. The packed executable and VM remain
the oracle until a piece of behaviour has been observed, recovered, and verified.
Methodology lives in [`AGENTS.md`](AGENTS.md), [`ARCHITECTURE.md`](ARCHITECTURE.md),
[`dos_re/AI_PORTING_CHARTER.md`](dos_re/AI_PORTING_CHARTER.md), and the
[`docs/`](docs/) tree; the original-address ledger is
[`docs/pre2/symbol_ledger.md`](docs/pre2/symbol_ledger.md).
