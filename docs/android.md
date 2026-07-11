# Android port — playable on a phone with the enhanced native runtime

This is the mobile path for the VM-less native game. It reuses the *entire* recovered engine unchanged —
Android is just another **host** driving the backend-agnostic core (a numpy framebuffer out, five input
flags in). The only game-facing addition is an on-screen control layer.

**Status:** in progress on branch `android-touch-port`.

- ✅ On-screen touch controls — pure resolver + pygame renderer, wired into `play_native.py`, unit-tested,
  and runnable on desktop with `--touch` (mouse = one finger).
- ✅ APK **built** — a working debug APK (`org.pre2port.pre2`, arm64-v8a, minSdk 26) was produced in WSL
  with the caveman-head launcher icon, the touch controls, and the game data bundled. See the build recipe
  below.
- ✅ Project icon — the extra-life caveman head (sprite 227, cream-outlined on transparent). Master at
  [`icon.png`](../icon.png); used for the native window and the APK launcher.
- ✅ Runs smoothly on device — after fixing the touch overlay (a full-window ~18 MB surface was re-uploaded to
  the GPU every frame the knob moved; now small static sprites via `Display.make_sprite`) and caching the
  background tile grid, gameplay went ~22 → ~52 fps with interpolation actually adding frames.
- ✅ Floating hidden joystick — the stick is invisible until you touch the left zone, then it appears under your
  thumb. It's a full analog circle; stick-**up** only registers as UP while **BASH** is also held (an upward
  bash), so walking up a slope never triggers a stray jump. JUMP is the dedicated button.
- ✅ Skippable intro — the intro titles (TITUS, then the PREHISTORIK-2 logo) play normally, but a tap/fire-key
  press during them skips straight to the menu (a skip during TITUS drops the logo too; the OLDIES credits are
  always fire-skippable). It's an opt-in `intro_skippable` setting (breaks boot accuracy — the titles never poll
  input in the VM) so the desktop default keeps the uninterruptible intro; the `--touch` build forces it on.
- ⬜ Device polish — asset import (SAF), pause/resume + audio focus, Back button, request 120 Hz mode +
  further `extract` optimization (the remaining ~15 ms/tick limiter), a clean arm64-only repackage.

> buildozer does **not** run on Windows. Build the APK on a Linux/WSL host (recipe below) or in CI.

## Controls

Landscape, thumbs on the bottom corners:

| Control | Location | Maps to | DOS scancode |
|---|---|---|---|
| **Virtual joystick** | left half (floating — hidden until you touch, plants under your thumb) | left / right / down (+ up only while BASH held) | `0x4B` `0x4D` `0x50` (`0x48`) |
| **JUMP** button | right, upper-left of the pair | up flag (the jump) | `0x48` |
| **BASH** button | right, lower-right of the pair | fire flag (the club attack) | `0x39` |

The joystick is a *floating, hidden* analog stick: it's invisible until you touch the left zone, which sets
its base under your thumb; then drag — a dead-zone plus a per-axis threshold turns the vector into clean
4/8-way directions. Pushing the stick **up** only emits UP *while BASH is held* (an upward bash) — so climbing
an up-diagonal never fires a stray jump. To actually jump, use the dedicated **JUMP** button; a JUMP tap feeds
the same responsive-controls jump buffer a keyboard/gamepad tap does.

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

## Building the APK

### In CI (recommended — no local Linux needed)

[`.github/workflows/android.yml`](../.github/workflows/android.yml) builds the debug APK on `ubuntu-latest`:

1. A fast **validate** gate — the touch unit tests + an import-check of the mobile entry (fails in seconds
   if the app code is broken).
2. A **build** job — buildozer / python-for-android downloads the SDK/NDK and cross-compiles numpy + pygame
   (tens of minutes cold), then uploads the `.apk` as a workflow artifact.

Trigger it from the repo's **Actions → Android APK → Run workflow**, or on push to `android-touch-port`.
The CI APK ships **no game data** (the `*.SQZ/*.TRK` aren't in git), so it launches to the "no data" screen
until you add data — its job is to prove the toolchain.

### Locally (Linux / WSL, with your data)

```bash
pip install buildozer
# put your legal *.SQZ / *.TRK into assets/ first (bundled into your private APK)
buildozer -v android debug
buildozer android deploy run logcat
```

The APK lands in `bin/`. `buildozer.spec` already pins the combo that works on a modern toolchain (see the
recipe notes below); a bleeding-edge python-for-android with default versions will **not** build as-is.

#### Build recipe / gotchas (what it took on Ubuntu 26.04 + NDK r28c)

The `buildozer.spec` captures the reproducible parts:

- `requirements = python3==3.11.9,numpy,pygame==2.6.1` — p4a's defaults (Python 3.14 + pygame 2.1.0) fail
  to compile (`longintrepr.h` is gone after 3.11). 3.11.9 + pygame 2.6.1 is the proven combo.
- `android.minapi = 26` — Bionic only declares `setgrent/getgrent/endgrent` at API ≥ 26, and NDK r28c's
  clang makes implicit declarations a hard error (CPython `grpmodule.c`).
- `android.archs = arm64-v8a` — 64-bit only (armeabi-v7a's numpy cross-build needs extra care).

Two fixes still live in the p4a checkout (`.buildozer/.../python-for-android/`) and are **not** captured by
the spec — redo them on a clean build (or pin p4a / use `p4a.local_recipes`):

1. `recipes/hostpython3/__init__.py`: set `version = "3.11.9"` (must match the target `python3`).
2. Install Cython into the built hostpython so pygame 2.6.1 can cythonise:
   `.../other_builds/hostpython3/desktop/hostpython3/native-build/root/usr/local/bin/python -m pip install "cython<3.1"`.

Also accept the SDK licenses non-interactively before the first build (buildozer's legacy `sdkmanager`
mis-parses `--licenses` under Java 17): write the canonical hash files into
`~/.buildozer/android/platform/android-sdk/licenses/android-sdk-license`.

**Do a clean rebuild after any version/arch change — do not reuse a build dir.** Every incremental shortcut
here caused a stale-artifact crash on device that only `adb logcat` revealed:

- `libmain.so` links `libpython<ver>.so` at bootstrap-build time → stale bootstrap ⇒ `dlopen failed: library
  "libpython3.14.so" not found` even though the APK ships `libpython3.11.so`.
- `rm other_builds/<pkg>` does **not** remove the installed copy in `python-installs/…/<pkg>` — p4a then keeps
  the old (3.14-linked) numpy/pygame `.so` and never rebuilds it.
- Renaming the platform build dir (e.g. dropping an arch) leaves the **old absolute path baked into
  hostpython's `pip3` shebang**, so later meson/pip steps fail with `exec: …/python: not found`.

The reliable recipe: `rm -rf .buildozer/android/platform/build-*` (keep `android-sdk`, `android-ndk-*`, and
the `python-for-android` clone) and rebuild from scratch. Verify before shipping — `llvm-readelf -d` on
`lib/arm64-v8a/libmain.so`, `numpy/_core/_multiarray_umath.so`, and `pygame/*.so` should all show
`NEEDED  libpython3.11.so` and no unresolved symbols.

The `python-for-android` clone / build tree still carries hand-edits that a clean checkout needs (not yet
moved to a committed `p4a.local_recipes`) — the reproducibility gap to close next:

1. `recipes/hostpython3/__init__.py`: `version = "3.11.9"` (must match target `python3`).
2. `recipes/pygame/__init__.py`: install Cython in `build_arch` (`install_hostpython_prerequisites(["Cython<3.1"])`,
   pygame 2.6.1 needs it) **and** add `src_c/simd_blitters_sse2.c` + `src_c/simd_blitters_avx2.c` to the
   `surface` module's Setup line (the Android Setup omits them → undefined SSE2/AVX2 symbols on ARM; the
   files self-gate to NEON via `sse2neon.h`, no extra `-D` needed).
3. **Music decoder**: SDL2_mixer ships with no MOD/tracker decoder, so the `.TRK` modules are silent. In the
   SDL2 bootstrap's `jni/SDL2_mixer`: clone `github.com/libsdl-org/libmodplug` (branch `v0.8.9.0-SDL`, which
   carries the `Android.mk`) into `external/libmodplug`, set `SUPPORT_MOD_MODPLUG ?= true` in `Android.mk`,
   and force the bootstrap to recompile. The existing enhanced `mixer.music` path then plays the modules.

Mobile presentation: the touch build defaults the enhancements ON (widescreen, true-widescreen,
interpolation, smooth transitions, stereo SFX, responsive controls) since there's no F10 menu on a phone —
see the `args.touch` block in `play_native.py`.

### First-launch behaviour without data

`main.py` catches `play_native`'s missing-data fail-loud and shows a fullscreen "copy your *.SQZ/*.TRK
files" message instead of instant-exiting — so the app always launches to *something*. Proper first-run
import (a Storage-Access-Framework picker) is the next milestone.

### Things to verify on the first real build

- Whether the p4a `pygame` recipe includes `pygame._sdl2.video`. If not, `display.py`'s software fallback
  carries the present path (fine at 320×200).
- Per-tick Python performance on a real device (no PyPy on Android). The tick is light (~23 Hz, 320×200)
  but must be profiled, especially on low-end phones.
- The `ArtemSBulgakov/buildozer_action` pin in the workflow — if it has drifted, swap in a maintained
  buildozer action or a manual `pip install buildozer && buildozer android debug` step.

## Next milestones

1. **First green APK** — boot to the TITUS titles on a device (the toolchain de-risker).
2. **Asset import** — a first-run SAF file picker so the APK ships no game data.
3. **Device lifecycle** — pause/resume, audio focus, Back button, landscape lock, perf pass.
4. **Sign & package.**
