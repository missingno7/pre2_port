# Android port — playable on a phone with the enhanced native runtime

This is the mobile path for the VM-less native game. It reuses the *entire* recovered engine unchanged —
Android is just another **host** driving the backend-agnostic core (a numpy framebuffer out, five input
flags in). The only game-facing addition is an on-screen control layer.

**Status:** in progress on branch `android-touch-port`.

- ✅ On-screen touch controls — pure resolver + pygame renderer, wired into `play_native.py`, unit-tested,
  and runnable on desktop with `--touch` (mouse = one finger).
- 🚧 APK build — `buildozer.spec` + `main.py` entry point are scaffolded but **not yet built** end-to-end
  (buildozer needs a Linux/WSL host with the Android SDK/NDK).
- ⬜ Device polish — asset import (SAF), pause/resume + audio focus, Back button, perf profiling.

## Controls

Landscape, thumbs on the bottom corners:

| Control | Location | Maps to | DOS scancode |
|---|---|---|---|
| **Virtual joystick** | left half (floating — plants under your thumb) | left / right / up / down | `0x4B` `0x4D` `0x48` `0x50` |
| **JUMP** button | right, upper-left of the pair | up flag (the jump) | `0x48` |
| **BASH** button | right, lower-right of the pair | fire flag (the club attack) | `0x39` |

The joystick is a *floating* analog stick: touch anywhere in the left zone to set its base, then drag — a
dead-zone plus a per-axis threshold turns the vector into clean 4/8-way directions. JUMP is also reachable
by pushing the stick up; the dedicated button just makes platforming precise. A JUMP tap feeds the same
responsive-controls jump buffer a keyboard/gamepad tap does.

### Why this stays byte-exact

The touch layer is a pure **host input** adapter: it only writes the same DC1 key-table flags a real
keyboard would (`pre2.native.input.set_key`). It never touches game state, so gameplay, demos, and the
byte-exact oracle are all unaffected — exactly like the gamepad path.

## Code map

| File | Role | pygame? |
|---|---|---|
| [`pre2/native/touch.py`](../pre2/native/touch.py) | Pure resolver: finger dict + window size → 5 flags + a render model. Stateful (per-finger ownership, floating-stick base). | no |
| [`scripts/touch_overlay.py`](../scripts/touch_overlay.py) | Host: translates `FINGER*` / mouse events → finger dict, ticks the resolver, paints the controls (cached). | yes |
| [`scripts/play_native.py`](../scripts/play_native.py) | Wires it in behind `--touch`: events in `pump()`, flags OR'd into `drive_input()`, overlay drawn in `present()`. | yes |
| [`main.py`](../main.py) | p4a entry point → `play_native.main([])`. | — |
| [`buildozer.spec`](../buildozer.spec) | APK build config (numpy + pygame; landscape; fullscreen). | — |

This keeps the project's layering rule intact: `pre2/` never imports pygame; all SDL/pygame lives in
`scripts/` (and `main.py`).

## Try the controls now (desktop)

```bash
python scripts/play_native.py --touch      # the mouse acts as a single finger
```

The joystick + JUMP/BASH draw over the game; click-drag the left half to move, click a button to act.
`--no-touch` forces them off. On Android the controls are on by default (no flag needed).

## Building the APK (untested scaffold)

Needs a Linux or WSL host:

```bash
pip install buildozer
# put your legal *.SQZ / *.TRK into assets/ first (bundled into your private APK)
buildozer -v android debug
buildozer android deploy run logcat
```

Things to verify on the first build:

- Whether the p4a `pygame` recipe includes `pygame._sdl2.video`. If not, `display.py`'s software fallback
  carries the present path (fine at 320×200).
- Per-tick Python performance on a real device (no PyPy on Android). The tick is light (~23 Hz, 320×200)
  but must be profiled, especially on low-end phones.

## Next milestones

1. **First green APK** — boot to the TITUS titles on a device (the toolchain de-risker).
2. **Asset import** — a first-run SAF file picker so the APK ships no game data.
3. **Device lifecycle** — pause/resume, audio focus, Back button, landscape lock, perf pass.
4. **Sign & package.**
