# Buildozer / python-for-android config for the Prehistorik 2 VM-less port (Android).
#
# STATUS: scaffold. This declares the APK build; it has NOT yet been built end-to-end (buildozer needs a
# Linux/WSL host with the Android SDK/NDK). Treat the recipe versions below as a starting point to pin.
#
#   Build:   pip install buildozer  &&  buildozer -v android debug
#   Deploy:  buildozer android deploy run logcat
#
# Game data: the *.SQZ / *.TRK files are user-owned and NOT in git. To bundle YOUR legal copy into YOUR
# private APK, drop them in assets/ (source.include_patterns picks them up). A later milestone replaces
# this with a first-run Storage-Access-Framework importer so the APK ships no game data.

[app]
title = Prehistorik 2
package.name = pre2
package.domain = org.pre2port

source.dir = .
# p4a's entry point is always main.py at the source root (which this repo has) — there is no spec key for it.
# Only Python sources + the game data get packaged (no pyc, no oracle VM, no tests/artifacts).
source.include_exts = py
source.include_patterns = assets/*.SQZ,assets/*.TRK,assets/*.sqz,assets/*.trk
source.exclude_dirs = tests,dos_re,artifacts,docs,dist,.git,.github,.idea,.pytest_cache,pre2/probes,venv,.venv

version = 0.1.0

# numpy + pygame are the ONLY runtime deps (README) — both have p4a recipes. dos_re / cffi are oracle-only
# and never shipped, so nothing exotic to cross-compile.
requirements = python3,numpy,pygame

orientation = landscape
fullscreen = 1

# Modern 64-bit + legacy 32-bit. Bump api/ndk to whatever your installed SDK/NDK provides.
android.archs = arm64-v8a,armeabi-v7a
android.api = 34
android.minapi = 24
android.allow_backup = 1

# No special permissions needed to play from bundled data. (A SAF importer would add READ access later.)
android.permissions =

[buildozer]
log_level = 2
warn_on_root = 1
