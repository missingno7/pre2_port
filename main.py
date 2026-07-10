"""Android / python-for-android entry point.

python-for-android runs this module as the app's main. It puts the repo root and ``scripts/`` on
``sys.path`` and hands off to the ordinary native runner (``scripts/play_native.py``) — with the
on-screen touch controls auto-enabled (``play_native`` detects the p4a ``ANDROID_ARGUMENT`` env var).

Desktop users keep launching ``python scripts/play_native.py`` directly; this file exists only for the
mobile package, so the desktop entry point is untouched.

If the game data (``*.SQZ`` / ``*.TRK``) isn't bundled, ``play_native`` fails loud with a missing-data
``SystemExit`` before it opens a window — which on a phone reads as an instant crash. We catch exactly
that case and show a readable "where to put your data" screen instead, so the app always *launches* to
something (the proper first-run importer is a later milestone).
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


def _no_data_screen(message: str) -> None:
    """Fullscreen fallback shown when the game data is missing — the app launched fine, it just has
    nothing to play yet. Tap or press any key (or Back) to quit."""
    import pygame
    pygame.init()
    pygame.font.init()
    screen = pygame.display.set_mode((0, 0))          # fullscreen on Android
    w, h = screen.get_size()
    font = pygame.font.Font(None, max(20, h // 22))
    small = pygame.font.Font(None, max(16, h // 30))
    lines = [
        ("Prehistorik 2", font, (235, 235, 245)),
        ("", small, (0, 0, 0)),
        ("No game data found.", small, (255, 200, 120)),
        ("Copy your legally-owned *.SQZ and *.TRK files", small, (210, 210, 220)),
        ("(from a Prehistorik 2 install) into the app, then relaunch.", small, (210, 210, 220)),
        ("", small, (0, 0, 0)),
        (message, small, (150, 160, 175)),
        ("", small, (0, 0, 0)),
        ("Tap to quit.", small, (150, 160, 175)),
    ]
    clock = pygame.time.Clock()
    running = True
    while running:
        for ev in pygame.event.get():
            if ev.type in (pygame.QUIT, pygame.KEYDOWN, pygame.FINGERDOWN, pygame.MOUSEBUTTONDOWN):
                running = False
        screen.fill((22, 26, 33))
        y = h // 4
        for text, fnt, color in lines:
            if text:
                surf = fnt.render(text, True, color)
                screen.blit(surf, surf.get_rect(center=(w // 2, y)))
            y += font.get_height()
        pygame.display.flip()
        clock.tick(30)
    pygame.quit()


if __name__ == "__main__":
    from play_native import main  # path set up above

    try:
        raise SystemExit(main([]))
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, str) and "SPRITES.SQZ" in code:   # the missing-data fail-loud from play_native
            _no_data_screen(code)
            raise SystemExit(0)
        raise
