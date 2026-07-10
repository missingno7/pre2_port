"""Android / python-for-android entry point.

python-for-android runs this module as the app's main. It puts the repo root and ``scripts/`` on
``sys.path`` and hands off to the ordinary native runner (``scripts/play_native.py``) — with the
on-screen touch controls auto-enabled (``play_native`` detects the p4a ``ANDROID_ARGUMENT`` env var).

Desktop users keep launching ``python scripts/play_native.py`` directly; this file exists only for the
mobile package, so the desktop entry point is untouched.
"""
import os
import sys

_ROOT = os.path.dirname(os.path.abspath(__file__))
for _p in (_ROOT, os.path.join(_ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Lock the SDL window to landscape before pygame initialises (buildozer also sets orientation, but this
# covers the SDL layer too).
os.environ.setdefault("SDL_HINT_ORIENTATIONS", "LandscapeLeft LandscapeRight")

from play_native import main  # noqa: E402  (path set up above)

if __name__ == "__main__":
    raise SystemExit(main([]))
