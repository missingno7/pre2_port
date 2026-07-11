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


_PKG = "org.pre2port.pre2"


def _external_dir():
    """The app's external files dir (user-reachable, the on-screen 'put your files here' path). None if
    the platform API is unavailable — the caller also tries explicit fallbacks."""
    try:
        from jnius import autoclass
        act = autoclass("org.kivy.android.PythonActivity").mActivity
        f = act.getExternalFilesDir(None)
        if f is not None:
            return f.getAbsolutePath()
    except Exception:
        pass
    return f"/sdcard/Android/data/{_PKG}/files"


def _data_roots():
    """Ordered candidate dirs to look for the game data in — the external files dir, its explicit sdcard /
    emulated-storage spellings (getExternalFilesDir can hand back a path a straight ``os.path.exists`` on
    another spelling wouldn't match, and adb-pushed files land under whichever the shell used), then the
    app-private dir. De-duplicated, order-preserving."""
    roots, seen = [], set()
    for r in (_external_dir(),
              f"/sdcard/Android/data/{_PKG}/files",
              f"/storage/emulated/0/Android/data/{_PKG}/files",
              os.environ.get("ANDROID_PRIVATE")):
        if r and r not in seen:
            seen.add(r)
            roots.append(r)
    return roots


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


def _internal_files_dir():
    """The app's PRIVATE files dir — the one storage the app can always read/write and the SAF import copies
    into. Prefer the platform API, fall back to $ANDROID_PRIVATE (p4a sets it to the same path)."""
    try:
        from jnius import autoclass
        act = autoclass("org.kivy.android.PythonActivity").mActivity
        return act.getFilesDir().getAbsolutePath()
    except Exception:                                            # noqa: BLE001
        return os.environ.get("ANDROID_PRIVATE") or ""


def _find_data_root():
    """The first candidate root that holds SPRITES.SQZ, or None. Each check is logged to logcat."""
    for root in _data_roots():
        try:
            has = os.path.exists(os.path.join(root, "SPRITES.SQZ"))
        except Exception as e:                                    # noqa: BLE001
            sys.stderr.write(f"[pre2] data root check failed for {root}: {e}\n")
            continue
        sys.stderr.write(f"[pre2] data root {root}: SPRITES.SQZ {'FOUND' if has else 'missing'}\n")
        if has:
            return root
    return None


def _saf_copy_tree(dest_dir):
    """Launch Android's folder picker (ACTION_OPEN_DOCUMENT_TREE — no permission needed) and copy every
    ``*.SQZ`` / ``*.TRK`` from the chosen folder into ``dest_dir`` (the app's private storage). Returns the
    number of files copied. Raises on a hard failure; returns 0 if the user cancelled.

    The picker is a separate activity, so this launches it, then blocks the Python thread on a flag the
    p4a activity-result callback sets — pumping SDL so the resumed window doesn't ANR."""
    import time
    from jnius import autoclass
    from android import activity                                 # p4a: routes onActivityResult -> Python
    PythonActivity = autoclass("org.kivy.android.PythonActivity")
    Intent = autoclass("android.content.Intent")
    DocumentsContract = autoclass("android.provider.DocumentsContract")
    Document = autoclass("android.provider.DocumentsContract$Document")
    FileOutputStream = autoclass("java.io.FileOutputStream")
    act = PythonActivity.mActivity
    resolver = act.getContentResolver()
    REQ = 0x50524532                                             # 'PRE2'
    state = {"done": False, "count": 0, "error": None}

    def on_result(request_code, result_code, intent):
        try:
            if request_code != REQ or intent is None or intent.getData() is None:
                return
            tree = intent.getData()
            tree_id = DocumentsContract.getTreeDocumentId(tree)
            children = DocumentsContract.buildChildDocumentsUriUsingTree(tree, tree_id)
            cur = resolver.query(children, None, None, None, None)
            i_name = cur.getColumnIndex(Document.COLUMN_DISPLAY_NAME)
            i_id = cur.getColumnIndex(Document.COLUMN_DOCUMENT_ID)
            os.makedirs(dest_dir, exist_ok=True)
            n = 0
            while cur.moveToNext():
                name = cur.getString(i_name) or ""
                if not name.upper().endswith((".SQZ", ".TRK")):
                    continue
                child = DocumentsContract.buildDocumentUriUsingTree(tree, cur.getString(i_id))
                inp = resolver.openInputStream(child)
                out = FileOutputStream(os.path.join(dest_dir, name))
                buff = bytearray(65536)
                while True:
                    r = inp.read(buff)
                    if r == -1:
                        break
                    out.write(buff, 0, r)
                out.close(); inp.close()
                n += 1
            cur.close()
            state["count"] = n
        except Exception as e:                                   # noqa: BLE001
            state["error"] = f"{type(e).__name__}: {e}"
        finally:
            state["done"] = True

    activity.bind(on_activity_result=on_result)
    try:
        intent = Intent(Intent.ACTION_OPEN_DOCUMENT_TREE)
        act.startActivityForResult(intent, REQ)
        import pygame
        while not state["done"]:
            time.sleep(0.05)
            try:
                pygame.event.pump()                             # keep SDL alive across the pause/resume
            except Exception:                                   # noqa: BLE001
                pass
    finally:
        activity.unbind(on_activity_result=on_result)
    if state["error"]:
        raise RuntimeError(state["error"])
    return state["count"]


def _import_screen():
    """First-run importer: a tap launches the SAF folder picker; on success the files land in the app's
    private storage and we return True (boot the game). Cancel / no valid files / error -> False (the caller
    shows the 'no data' help). Android only — off Android there is nothing to import."""
    import pygame
    pygame.init(); pygame.font.init()
    screen = pygame.display.set_mode((0, 0))
    w, h = screen.get_size()
    fh = max(18, h // 26)
    font = pygame.font.Font(None, fh)
    small = pygame.font.Font(None, max(14, int(fh * 0.72)))
    clock = pygame.time.Clock()
    dest = _internal_files_dir()

    def draw(lines):
        screen.fill((18, 20, 26))
        y = fh
        for text, color, f in lines:
            screen.blit(f.render(text, True, color), (fh, y))
            y += int(f.get_height() * 1.35)
        pygame.display.flip()

    idle = [("Prehistorik 2 — import game files", (240, 236, 230), font),
            ("", (0, 0, 0), small),
            ("Tap to pick the folder that holds your *.SQZ / *.TRK files.", (205, 210, 220), small),
            ("They are copied into the app; you only do this once.", (150, 160, 175), small)]
    while True:
        tapped = False
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                return False
            if ev.type in (pygame.FINGERDOWN, pygame.MOUSEBUTTONDOWN, pygame.KEYDOWN):
                tapped = True
        if tapped:
            draw([("Opening the folder picker…", (240, 236, 230), font)])
            try:
                n = _saf_copy_tree(dest)
            except Exception as e:                              # noqa: BLE001
                sys.stderr.write(f"[pre2] SAF import failed: {e}\n")
                draw([("Import failed", (255, 180, 160), font), ("", (0, 0, 0), small),
                      (str(e)[:120], (220, 210, 205), small), ("Tap to try again.", (150, 160, 175), small)])
                _wait_tap(pygame, clock)
                continue
            if n > 0 and os.path.exists(os.path.join(dest, "SPRITES.SQZ")):
                sys.stderr.write(f"[pre2] SAF import: {n} files -> {dest}\n")
                return True
            msg = "No *.SQZ/*.TRK in that folder." if n == 0 else "SPRITES.SQZ missing from that folder."
            draw([("Nothing imported", (255, 210, 160), font), ("", (0, 0, 0), small),
                  (msg, (220, 210, 205), small), ("Tap to pick a different folder.", (150, 160, 175), small)])
            _wait_tap(pygame, clock)
        else:
            draw(idle)
        clock.tick(30)


def _wait_tap(pygame, clock):
    while True:
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                return
            if ev.type in (pygame.FINGERDOWN, pygame.MOUSEBUTTONDOWN, pygame.KEYDOWN):
                return
        clock.tick(30)


def _run():
    # The APK ships no game data. Find the user's data among the candidate roots; if none, offer the SAF
    # importer (Android) which copies the picked folder's *.SQZ/*.TRK into the app's private storage.
    root = _find_data_root()
    if root is None and ("ANDROID_ARGUMENT" in os.environ or hasattr(sys, "getandroidapilevel")):
        try:
            if _import_screen():
                root = _find_data_root()
        except Exception as e:                                   # noqa: BLE001 — importer must never hard-crash the app
            sys.stderr.write(f"[pre2] import screen error: {e}\n")

    from play_native import main
    return main(["--game-root", root] if root else [])


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
