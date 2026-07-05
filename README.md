# Prehistorik 2 — a recovered, VM-less source port

A faithful source port of the DOS game **Prehistorik 2**, reconstructed from the original `PRE2.EXE`
one verified piece at a time. The result is clean, standalone Python that plays the whole game with
**no emulator** — the custom `dos_re` real-mode VM is kept only as an *offline oracle* that proves the
recovered code matches the original byte-for-byte.

It is a **source port, not a remake**: every routine is recovered from the real binary and checked
against it, not re-imagined. And it is **not "the VM with a nicer front-end"** — the shipped game runs
no x86, loads no `PRE2.EXE`, and starts no emulator.

```
$ python scripts/play_native.py        # cold-boots the real game: titles → menu → levels → endings
```

## Architecture — layered by dependency, verified at every seam

The whole design is one idea: **separate *what the game does* from *where the bytes live* from *how it
was the original machine*, and prove each layer against the original as you go.** Everything flows in
one direction — higher layers depend on lower ones, never the reverse.

```
   ┌─────────────────────────────────────────────────────────────┐
   │  scripts/play_native.py        the product — no VM, no EXE   │   ← you run this
   └──────────────────────────────┬──────────────────────────────┘
                                  │ drives
   ┌──────────────────────────────▼──────────────────────────────┐
   │  pre2/native/     the VM-less runtime: NativeGameState +     │
   │                   frame driver + native VGA/audio + boot     │
   └──────────────────────────────┬──────────────────────────────┘
                                  │ reads / writes game state through
   ┌──────────────────────────────▼──────────────────────────────┐
   │  pre2/bridge/     the state seam: human-named views ⇄ the    │
   │                   DGROUP memory layout (dgroup_view.py)       │
   └──────────────────────────────┬──────────────────────────────┘
                                  │ calls
   ┌──────────────────────────────▼──────────────────────────────┐
   │  pre2/recovered/  THE GAME (pure): gameplay + render logic   │
   │  pre2/codecs/     asset decoders (SQZ / sprites / audio)     │
   │                   —— never imports cpu / mem / dos_re ——     │
   └──────────────────────────────┬──────────────────────────────┘
                                  │ decodes
   ┌──────────────────────────────▼──────────────────────────────┐
   │  game data (*.SQZ / *.TRK)  +  boot constants (boot_data.py) │
   └─────────────────────────────────────────────────────────────┘

   dos_re/   the reusable real-mode VM — stands OUTSIDE this stack as the
             OFFLINE ORACLE: it replays the original ASM to prove the recovered
             code byte-for-byte. Game-agnostic. Never shipped, never run at game time.
```

| Layer | Directory | What it holds | The rule that keeps it clean |
|---|---|---|---|
| **The game** | `pre2/recovered/`, `pre2/codecs/` | The recovered gameplay + render + audio logic and asset decoders — the actual source port. | **Pure.** No `cpu`/`mem`/`dos_re` imports. Reads state as human-named fields, never raw offsets. |
| **The state seam** | `pre2/bridge/` | Human-named *views* (`player.x`, `slot.sprite`) over the DOS data-segment layout, plus dataclass readers. | The **one place** a DGROUP byte offset is written down. Swappable backends (live image / verify overlay). |
| **The runtime** | `pre2/native/` | `NativeGameState` (the memory image), the per-frame driver, native VGA + audio, and the boot constants that replace the EXE. | Owns the machine so the recovered layer doesn't have to. VM-free. |
| **The oracle** | `dos_re/` | A reusable 8086 + hardware emulator. | **Verification only.** Knows nothing about Prehistorik 2. Never in the shipped game. |

Four invariants define the separation — and they're enforced, not aspirational:

1. **Dependency points down.** The pure game logic never reaches back to the VM, CPU, or segmented memory.
   The VM sits at the bottom as an oracle; nothing above it depends on it to *run*.
2. **Offsets are quarantined.** Gameplay code speaks `player.x` / `slot.sprite`; the DOS memory layout lives
   only in `pre2/bridge/dgroup_view.py`. See [`docs/pre2/state_view_layer.md`](docs/pre2/state_view_layer.md).
3. **The VM is an oracle, not the engine.** It replays the original ASM to *check* the recovered code; it is
   never started to *play* the game. No EXE, no emulator, no boot image at runtime.
4. **Everything is proven.** Each recovered routine is byte-exact against the original — carried in an
   `@oracle_link(...)` tag and auto-collected into [`docs/pre2/recovered_islands.md`](docs/pre2/recovered_islands.md)
   (a test fails if the code and the manifest drift). That manifest is the source of truth for what's recovered.

## What runs today

`play_native.py` cold-boots the entire game from the boot constants + the GOG assets, with no emulator:

> OLDIES credits → TITUS / PREHISTORIK-2 titles → menu → world-map (carte) → gameplay → level-end tally
> → next level → … → endings → game-over → restart.

Everything the frame touches is recovered source, verified against the ASM:

- **Gameplay** — the whole per-frame loop: player FSM + movement + collision, the object/entity state
  machine, the second pass, terrain/moving-platform entities, effects, the combat pass, and the level
  state machine (death / respawn / checkpoint / level-end / game-over / game-complete).
- **Rendering** — asset decode (SQZ: LZSS/LZW/Huffman+RLE), sprite/tile demux + classify + blit, the
  object-list draw, the frame renderer (tile-row / grid / scroll / page-flip), HUD, level-end iris,
  particles / fireflies / foreground z-order, parallax, and the LEVELG falling snow.
- **Audio** — digital SFX and per-level ProTracker music from the recovered banks.
- **Front-end** — intro, titles, attract animation, difficulty menu, password entry, the carte scroll-in,
  transitions and DAC fades.

### Known issues

Small and honest — **none block a normal cold-boot → credits playthrough:**

- **A few rare edge-case paths still fail loud** (`Pre2HybridGap`) rather than run: an exotic bonus-level
  table-warp variant, and two defensive camera-pan guards on cave transitions. The normal path never
  reaches them; failing loud is deliberate (an unrecovered path is never silently faked).
- **No known missing visuals.** (The long-listed "`0x9dc0` parallax blit" turned out to be the loader's
  level-data stash/restore through spare VRAM — a memory-reuse trick with no net effect, which native's
  pure asset decode correctly sidesteps.)
- **The state-view refactor is an in-progress internal sweep** (moving raw memory offsets out of the
  recovered logic into a named view layer). No gameplay or visual effect.

## Two runtimes: the product and the workbench

| | Entry point | VM? | For |
|---|---|---|---|
| **Native** (the product) | `scripts/play_native.py` | none | playing / shipping the recovered game |
| **Hybrid** (the workbench) | `scripts/play.py` | yes — as oracle | recovering & proving new behaviour against the live ASM |

The workbench is where a new piece is prepared and proven; the **same** recovered functions then run in
the native product with the hooks gone (*"hybrid is the workshop, native is the product"*). The workbench
never silently falls back to ASM — an unrecovered path **fails loud** (`Pre2HybridGap`) so the gap becomes
the next task. Verification replays a recorded demo through the ASM oracle and the native core **tick by
tick** and compares the full memory image; native reproduces the VM exactly (render/async offsets aside).

## Quickstart

```bash
# play the recovered game — no emulator, cold boot from the first screen
python scripts/play_native.py                     # full boot: titles → menu → play
python scripts/play_native.py --from-level 0      # debug: drop straight into LEVEL1
python scripts/play_native.py --snapshot <dir>    # resume a saved gameplay state

# ship it — a standalone folder (+ optional PyInstaller exe), with no VM or EXE
python scripts/deploy_native.py                   # build + smoke-test dist/pre2native/
python scripts/deploy_native.py --exe             # + PyInstaller (needs `pip install pyinstaller`)

# the workbench — hybrid runtime over the ASM oracle
python scripts/play.py --view                     # live viewer, recovered hooks over the VM
python scripts/play.py --view --verify-hooks      # lockstep contract check vs the ASM
python scripts/play.py --view --record-demo run1  # record a regression demo
```

Native controls: `SPACE` = advance / fire+jump, arrows or numpad = move, `ESC` = quit.

## Bring your own legal copy

This is a source-port **workbench**, not a redistribution. It needs the original Prehistorik 2 data files,
which are **not** in this repository — supply them from a copy you legally own.

- Target the **GOG.com DRM-free release**. Copy the game's `*.SQZ` / `*.TRK` data into [`assets/`](assets/),
  or point `--game-root` at your GOG install folder.
- All addresses and recovered logic are derived against that GOG build; another build has a different memory
  layout and will not line up.
- The native game (`play_native.py`) needs **only the data files**. The workbench (`play.py`, verification)
  also needs the GOG `PRE2.EXE` in `assets/` — the oracle.
- Never commit the game binary or data; they stay local to your checkout.

## Where to read more

- **Architecture & posture** — [`docs/pre2/recovery_architecture.md`](docs/pre2/recovery_architecture.md)
  (north star) and [`docs/pre2/recovery_lifecycle.md`](docs/pre2/recovery_lifecycle.md) (the recovery
  lifecycle and the VM-as-oracle destination).
- **The state seam** — [`docs/pre2/state_view_layer.md`](docs/pre2/state_view_layer.md) (human-named views,
  swappable backends, why offsets leave the logic but the byte-backed store is a legitimate release citizen).
- **What's recovered** — [`docs/pre2/recovered_islands.md`](docs/pre2/recovered_islands.md) (generated;
  source of truth) and the address ledger [`docs/pre2/symbol_ledger.md`](docs/pre2/symbol_ledger.md).
- **Contributor guardrails** — [`AGENTS.md`](AGENTS.md), [`ARCHITECTURE.md`](ARCHITECTURE.md), and the
  reusable method in [`dos_re/AI_PORTING_CHARTER.md`](dos_re/AI_PORTING_CHARTER.md).

> `dos_re/` stays game-agnostic on purpose — anything that knows Prehistorik 2 filenames, addresses, or
> formats lives under `pre2/`. That boundary is what makes the VM a reusable oracle instead of part of the game.
