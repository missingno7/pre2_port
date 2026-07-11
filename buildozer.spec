# Buildozer / python-for-android config for the Prehistorik 2 VM-less port (Android).
#
# STATUS: scaffold. This declares the APK build; it has NOT yet been built end-to-end (buildozer needs a
# Linux/WSL host with the Android SDK/NDK). Treat the recipe versions below as a starting point to pin.
#
#   Build:   pip install buildozer  &&  buildozer -v android debug
#   Deploy:  buildozer android deploy run logcat
#
# Game data: the *.SQZ / *.TRK files are user-owned and are NEVER bundled into the APK (this spec excludes
# them by construction — include_exts has no SQZ/TRK and assets/ is excluded). The app loads the data from
# its external files dir on the phone: copy your legal GOG/original files to
#   /sdcard/Android/data/org.pre2port.pre2/files/        (adb push assets/*.SQZ assets/*.TRK <that dir>)
# On first launch without data the app shows exactly that path on screen. A first-run Storage-Access-
# Framework importer is the planned convenience on top.

[app]
title = Prehistorik 2
package.name = pre2
package.domain = org.pre2port

# Launcher icon: the extra-life caveman head (from sprite 227), cream-outlined on transparent so it reads
# on any launcher. buildozer generates the mipmap densities from this one PNG.
icon.filename = %(source.dir)s/icon.png

source.dir = .
# p4a's entry point is always main.py at the source root (which this repo has) — there is no spec key for it.
# Python sources + the icon ONLY — deliberately NO SQZ/TRK extensions and assets/ excluded, so a built APK
# can never ship game data even when a local checkout has a legal copy in assets/ for desktop play.
source.include_exts = py,png
source.exclude_dirs = assets,tests,dos_re,artifacts,docs,dist,bin,.git,.github,.idea,.pytest_cache,pre2/probes,venv,.venv

version = 0.1.0

# numpy + pygame are the ONLY runtime deps (README) — both have p4a recipes. dos_re / cffi are oracle-only
# and never shipped, so nothing exotic to cross-compile.
# Pin the target Python + pygame: the bleeding-edge p4a defaults to Python 3.14 + pygame 2.1.0, which fails
# to compile (pygame 2.1.0's _sdl2/sdl2.c needs longintrepr.h, gone after Python 3.11). 3.11.9 + pygame 2.6.1
# is the proven-good combo (same as the desktop build).
requirements = python3==3.11.9,numpy,pygame==2.6.1

orientation = landscape
fullscreen = 1

# arm64-v8a only: every phone from the last ~8 years is 64-bit. (Add armeabi-v7a back for legacy 32-bit
# devices — note numpy's cross-build there needed extra care.)
android.archs = arm64-v8a
android.api = 34
# minapi 26 (Android 8.0): Bionic only declares setgrent/getgrent/endgrent at API >= 26, and NDK r28c's
# clang makes implicit declarations a hard error — CPython's grpmodule.c fails to compile below 26.
android.minapi = 26
android.allow_backup = 1

# No permissions needed: the game data lives in the app's OWN external files dir (app-specific storage is
# readable/writable by the owning app without any permission; users reach it via adb push or a file manager
# that can enter Android/data). A SAF importer would not need permissions either.
android.permissions =

[buildozer]
log_level = 2
warn_on_root = 1
