"""Android / python-for-android entry point.

python-for-android runs this module as the app's main. It puts the repo root and ``scripts/`` on
``sys.path`` and hands off to the ordinary native runner (``scripts/play_native.py``) — with the
on-screen touch controls auto-enabled (``play_native`` detects the p4a ``ANDROID_ARGUMENT`` env var).

Desktop users keep launching ``python scripts/play_native.py`` directly; this file exists only for the
mobile package, so the desktop entry point is untouched.

Game data: the *.SQZ / *.TRK files are bundled in the APK, but if you drop your own copy into the app's
external files dir (``/sdcard/Android/data/org.pre2port.pre2/files/``) they take precedence — a
zero-UI stand-in for the eventual first-run importer.

On any crash we write the full traceback to ``pre2_crash.log`` in that same external dir (retrievable with
a file manager, no adb) and show it on screen, so a silent close becomes diagnosable.
"""
import os
import sys
import traceback

_ROOT = os.path.dirname(os.path.abspath(__file__))
for _p in (_ROOT, os.path.join(_ROOT, "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# SDL's orientations hint: the real hint STRING is "SDL_IOS_ORIENTATIONS" (the SDL_HINT_ORIENTATIONS C
# constant's value — iOS-named for historical reasons, but SDLActivity honors it on Android). Without it,
# SDLActivity's own setRequestedOrientation call at window creation requests FULL_SENSOR for fullscreen
# windows — overriding the manifest's landscape lock and re-enabling portrait (which makes the game ugly).
# android_host.lock_landscape() re-asserts it Java-side as belt-and-braces.
os.environ.setdefault("SDL_IOS_ORIENTATIONS", "LandscapeLeft LandscapeRight")
os.environ.setdefault("SDL_ANDROID_TRAP_BACK_BUTTON", "1")   # deliver Back as K_AC_BACK (the pause dialog)
#   instead of letting the OS finish the activity — play_native opens the Resume/Main-menu/Exit dialog on it.


def _external_dir():
    """The app's external files dir (user-reachable via a file manager). None if unavailable."""
    try:
        from jnius import autoclass
        act = autoclass("org.kivy.android.PythonActivity").mActivity
        f = act.getExternalFilesDir(None)
        if f is not None:
            return f.getAbsolutePath()
    except Exception:
        pass
    guess = "/sdcard/Android/data/org.pre2port.pre2/files"
    return guess if os.path.isdir("/sdcard") else os.environ.get("ANDROID_PRIVATE")


def _write_log(text):
    for base in {_external_dir(), os.environ.get("ANDROID_PRIVATE")}:
        if not base:
            continue
        try:
            os.makedirs(base, exist_ok=True)
            with open(os.path.join(base, "pre2_crash.log"), "w", encoding="utf-8") as f:
                f.write(text)
            return os.path.join(base, "pre2_crash.log")
        except Exception:
            continue
    return None


def _message_screen(lines):
    """Fullscreen text (a title + wrapped body). Tap / key / Back quits."""
    import pygame
    pygame.init(); pygame.font.init()
    screen = pygame.display.set_mode((0, 0))
    w, h = screen.get_size()
    fh = max(16, h // 34)
    font = pygame.font.Font(None, fh)
    clock = pygame.time.Clock()
    running = True
    while running:
        for ev in pygame.event.get():
            if ev.type in (pygame.QUIT, pygame.KEYDOWN, pygame.FINGERDOWN, pygame.MOUSEBUTTONDOWN):
                running = False
        screen.fill((20, 22, 28))
        y = fh
        for text, color in lines:
            screen.blit(font.render(text[:200], True, color), (fh, y))
            y += fh
        pygame.display.flip()
        clock.tick(30)
    pygame.quit()


def _run():
    # Prefer user-supplied data in the external dir; else the bundled assets/ (play_native's default).
    argv = []
    ext = _external_dir()
    if ext and os.path.exists(os.path.join(ext, "SPRITES.SQZ")):
        argv = ["--game-root", ext]

    from play_native import main
    return main(argv)


if __name__ == "__main__":
    try:
        rc = _run()
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, str) and "SPRITES.SQZ" in code:      # missing-data fail-loud
            _message_screen([
                ("Prehistorik 2 — no game data", (245, 235, 235)),
                ("", (0, 0, 0)),
                ("Put your *.SQZ / *.TRK files in:", (210, 210, 220)),
                (f"{_external_dir()}", (150, 200, 255)),
                ("", (0, 0, 0)),
                ("Tap to quit.", (150, 160, 175)),
            ])
            raise SystemExit(0)
        if code not in (0, None):
            tb = f"SystemExit: {code}"
            path = _write_log(tb)
            _message_screen([("Prehistorik 2 exited", (245, 220, 180)), (str(code)[:180], (220, 220, 220)),
                             ("", (0, 0, 0)), (f"log: {path}", (150, 160, 175)), ("Tap to quit.", (150, 160, 175))])
        raise
    except BaseException:                                         # noqa: BLE001 — surface ANY crash on-device
        tb = traceback.format_exc()
        sys.stderr.write(tb)                                     # -> logcat
        path = _write_log(tb)
        last = [ln for ln in tb.strip().splitlines() if ln.strip()][-3:]
        lines = [("Prehistorik 2 crashed", (255, 180, 160)), ("", (0, 0, 0))]
        lines += [(ln, (225, 210, 200)) for ln in last]
        lines += [("", (0, 0, 0)), (f"full log: {path}", (150, 200, 255)), ("Tap to quit.", (150, 160, 175))]
        try:
            _message_screen(lines)
        except Exception:
            pass
        raise SystemExit(1)
