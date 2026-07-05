"""Play Prehistorik 2 with the VM-LESS native core — COLD BOOT from the OLDIES screen through the whole flow.

The standalone runner, with NO emulator anywhere: from the BOOT CONSTANTS (pre2/native/boot_data.py — the game's
initialized data segment, no EXE needed) + the GOG ``*.SQZ`` assets, it drives the recovered FRONT-END flow (OLDIES credits ->
TITUS title -> PREHISTORIK-2 title -> menu -> world map -> level) and then the recovered GAMEPLAY — no x86 is
interpreted and ``PRE2.EXE`` is never executed at runtime. This is the VM-less counterpart of ``play.py --view``:
it starts at the very first screen, exactly like the real game, and runs forward until it hits a not-yet-recovered
gap (where it stops and reports, rather than silently faking anything).

    python scripts/play_native.py                  # full cold start: OLDIES -> titles -> ... (the real boot)
    python scripts/play_native.py --from-level 0    # DEBUG: skip the front-end, drop straight into LEVEL1 gameplay
    python scripts/play_native.py --fps 30          # gameplay tick rate (front-end runs at its native 70Hz)

Controls: SPACE = advance the OLDIES screen / fire+jump in game; arrow keys / numpad = move; ESC = quit.

THE BOOT STATE is pure constants (pre2/native/boot_data.py, generated once by pre2/probes/extract_boot_data.py —
the VM's only remaining, workbench-side role). No PRE2.EXE and no boot image at runtime: copy the package +
the game data anywhere and run.

STATUS: plays the full flow VM-less — OLDIES + the two title screens + attract animation + the "press 1/2" menu +
password entry + the mode-select world-map + the CARTE scroll-in, then GAMEPLAY: the whole per-frame loop (player
FSM/movement/collision, the object + second passes, terrain/effects, the 88D7 combat pass, and the 4C69 level
state machine — death / respawn / checkpoint / level-end tally / game-over / game-complete), with digital SFX and
per-level music, level transitions, and the LEVELG snow/wind. Verified byte-exact vs the pure-ASM oracle tick by
tick. ``--from-level`` boots a level directly for testing. Known residuals are small and fail loud (a few rare
edge-case paths, e.g. the game-over-via-respawn tail; the level-end count-up *cutscene* animation is deferred).
When the runner reaches an unrecovered gap it prints it and holds the last frame.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

DS = 0x1A0F << 4
_FRONT_END_FPS = 70           # the front-end runs at the VGA retrace rate (its FrontEndScene frames are per-retrace)
VIEWPORT_H = 176              # gameplay viewport rows (the HUD strip below never scrolls / fades)
_VFADE_MID = 88               # the 30C6 vertical fade's two bands meet here (fully closed)
# Smooth-transition durations in wall-clock SECONDS (present-time driven -> frame-rate-independent).
_REVEAL_S = 0.45             # level-start / checkpoint / cave center-out curtain reveal
_IRIS_S = 1.0                # level-end circular iris close
_FADE_S = 0.35               # cave / death vertical fade-to-black
_CAVE_BLACK_S = 0.12         # the brief black hold while the camera pans behind the fade
_TRANSITION_FPS = 30          # curtains/fades: the VM's 3054/30C6 are vsync-paced sub-frame effects that span
#                               ~20 retraces (~0.34s); presenting the ~11 reveal steps at 70Hz was ~2x too fast.
TICK_HZ = 70.0 / 3.0          # the game's own tick rate: the 1C6F frame limiter waits 3 PIT/retrace periods per
#                               main-loop tick (70Hz VGA / 3) — the faithful gameplay pacing (~23.33Hz). The old
#                               default of 24 was a ~3% -fast approximation of this.


class DemoInput:
    """Replay a recorded input demo's scancodes into the VM-less runtime for HANDS-FREE watching.

    A recorded demo is a list of make/break scancode events keyed to a per-frame ``boundary`` counter (the
    hybrid recorder's present-frame index). Here the boundary is advanced once per NATIVE displayed frame and the
    make/break events are turned back into a held-key set, which the runtime writes into DC1's key table exactly
    as a live keyboard would. This is APPROXIMATE across the front-end (native scene timing differs from the
    recording, so menu/title waits can drift); it is faithful for gameplay, where the frame is the game tick.
    Live keys are merged on top, so you can always nudge the flow (e.g. tap SPACE past a drifted OLDIES wait)."""

    STD = (0x39, 0x48, 0x50, 0x4D, 0x4B, 0x02, 0x03)   # fire, up, down, right, left, '1', '2' (DC1 sources)

    def __init__(self, playback):
        self.events = list(playback.events)            # already sorted by (boundary, seq)
        self.i = 0
        self.boundary = 0
        self.held: set[int] = set()

    def step(self) -> None:
        """Advance one native frame: apply every event due at/under the current boundary, then bump it."""
        while self.i < len(self.events) and self.events[self.i].boundary <= self.boundary:
            ev = self.events[self.i]; self.i += 1
            if ev.kind == "scan":
                sc = ev.value & 0xFF
                (self.held.discard if sc & 0x80 else self.held.add)(sc & 0x7F)
        self.boundary += 1

    @property
    def finished(self) -> bool:
        return self.i >= len(self.events)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Play PRE2 with the VM-less native core (cold boot from OLDIES)")
    ap.add_argument("--from-level", type=int, default=None,
                    help="DEBUG: skip the front-end and boot this 0-based level directly (0 -> LEVEL1)")
    ap.add_argument("--snapshot", default=None,
                    help="DEBUG: seed gameplay from a savestate dir (memory_1mb.bin) instead of cold-booting")
    ap.add_argument("--play-demo", default=None,
                    help="replay a recorded demo. If DIR/game_tick_demo.bin exists (created once by "
                         "scripts/verify_native_tick_demo.py DIR), the replay is DETERMINISTIC: seeded from the "
                         "oracle's first gameplay tick, per-tick keys injected, gameplay digest checked vs the VM "
                         "every tick. Otherwise falls back to APPROXIMATE scancode replay (cold boot + live keys "
                         "merged; front-end timing drifts).")
    ap.add_argument("--game-root", default=str(ROOT / "assets"),
                    help="folder with the game data files (*.SQZ/*.TRK — e.g. the GOG Prehistorik 2 install "
                         "dir); default: the repo's assets/")
    ap.add_argument("--fps", type=float, default=None,
                    help="gameplay tick-rate cap; default = the faithful 70/3 Hz (~23.33 — the original's "
                         "main loop waits 3 VGA retraces per tick)")
    ap.add_argument("--scale", type=int, default=2)
    ap.add_argument("--debug", action="store_true",
                    help="show the Develop tab in the F10 overlay menu (level select, god mode — cheats; "
                         "hidden from the end-user product by default)")
    args = ap.parse_args(argv)
    if args.fps is None:
        args.fps = TICK_HZ                              # faithful pacing unless the user overrides

    import numpy as np
    import pygame
    from pre2.native.vga import NativeVGA
    from pre2.gaps import Pre2HybridGap
    from pre2.native.boot_data import build_boot_memory
    from pre2.native.cold_boot import native_cold_boot
    from pre2.native.front_end import native_front_end
    from pre2.native.input import init_keyboard_input, set_key
    from pre2.native.render import native_load_level_palette
    from pre2.native.runtime import (native_exit_anim, native_frame_step, native_frame_step_tagged,
                                      native_iris_close, native_level_reveal)
    from pre2.native.state import NativeGameState
    from sdl_view import front_end_scene_to_rgb, render_planar_rgb_from_planes

    gr = str(Path(args.game_root))
    if not (Path(gr) / "SPRITES.SQZ").exists():
        raise SystemExit(f"--game-root {gr}: no SPRITES.SQZ here — point it at the Prehistorik 2 data folder")
    demo = None
    if args.play_demo and not (Path(args.play_demo) / "game_tick_demo.bin").exists():
        # APPROXIMATE scancode fallback only — the deterministic tick replay below doesn't need the input demo.
        # Lazy import: dos_re.input_demo is VM-side plumbing the deployed standalone doesn't ship (fails loud here).
        from dos_re.input_demo import InputDemoPlayback
        demo = DemoInput(InputDemoPlayback.load(args.play_demo))
        print(f"--play-demo: replaying {len(demo.events)} input events (hands-free; live keys merged, ESC quits)")

    # DPI awareness BEFORE any window exists: on Windows with display scaling (e.g. 150%) an un-aware process
    # gets the LOGICAL desktop size, so a borderless-fullscreen window doesn't cover the physical screen and
    # its (0,0) placement drifts. Make the process per-monitor DPI-aware so get_desktop_sizes() = real pixels.
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)   # PER_MONITOR_AWARE_V2
        except Exception:                                    # noqa: BLE001 — older Windows
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:                                # noqa: BLE001
                pass
    import os as _os
    _os.environ.setdefault("SDL_WINDOWS_DPI_AWARENESS", "permonitorv2")   # SDL's own DPI path (>= 2.24)
    pygame.init()
    view = {"screen": pygame.display.set_mode((320 * args.scale, 200 * args.scale), pygame.RESIZABLE),
            "borderless": False}
    clock = pygame.time.Clock()

    def detect_display_hz() -> float:
        """The monitor's REAL current refresh rate. pygame's get_current_refresh_rate is unreliable (often
        reports 60 regardless), so on Windows read the actual mode via GDI VREFRESH first."""
        if sys.platform == "win32":
            try:
                import ctypes
                hdc = ctypes.windll.user32.GetDC(None)
                hz = ctypes.windll.gdi32.GetDeviceCaps(hdc, 116)         # VREFRESH (current display refresh)
                ctypes.windll.user32.ReleaseDC(None, hdc)
                if hz and hz > 1:                                        # 0/1 = "default"; a real panel is >1
                    return float(hz)
            except Exception:                                            # noqa: BLE001
                pass
        try:
            r = float(pygame.display.get_current_refresh_rate())        # pygame >= 2.2 (fallback)
            if r > 0:
                return r
        except Exception:                                                # noqa: BLE001 — older pygame / headless
            pass
        return 60.0                                                      # safe fallback
    # The MONITOR refresh rate — the presentation clock interpolation/smooth transitions present at. The game
    # TICK stays locked at TICK_HZ regardless; only how many presented frames per tick depend on this.
    display_hz = detect_display_hz()
    print(f"display: {display_hz:.0f} Hz (game tick {TICK_HZ:.2f} Hz)")
    ref = {"running": True, "last": None, "last_scan": 0, "p_prev": False, "display_hz": display_hz,
           "menu_request": False, "switch_level": None, "tick_count": 0}

    # --- the end-user settings (the F10 menu edits these; the CLI shrinks to dev flags) -----------------
    import json
    settings_path = Path(gr) / "pre2native_settings.json"
    settings = {"integer_scale": False, "fps_overlay": False, "music": True, "sfx": True, "god": False,
                "interpolation": False, "frame_cap": 0,   # 0 = Display (detected Hz), -1 = Uncapped, else Hz
                "widescreen": False, "fullscreen": False, "true_widescreen": False,
                "smooth_transitions": False, "hud_align": "center",   # widescreen HUD: left / center / right
                "overlay_scale": "auto",   # F10 menu size: "auto" (by window) or 100/150/200/300 (%)
                "widescreen_bg": "stretch"}   # widescreen backdrop margins: stretch / mirror / black
    try:
        settings.update({k: v for k, v in json.loads(settings_path.read_text()).items() if k in settings})
    except Exception:                                                     # noqa: BLE001 — first run / unreadable
        pass
    settings["god"] = False                                              # a cheat never persists across runs

    def save_settings():
        try:
            persist = {k: v for k, v in settings.items() if k != "god"}
            settings_path.write_text(json.dumps(persist, indent=1))
        except Exception as e:                                            # noqa: BLE001 — read-only game dir
            print(f"(settings not saved: {e})")

    def _win_monitor_rect():
        """(x, y, w, h) PHYSICAL-pixel rect of the monitor the current window sits on (Windows; the process is
        DPI-aware so this is real pixels), or None. Lets the borderless window cover the RIGHT monitor exactly
        on multi-monitor / scaled setups instead of guessing (0,0)+primary-size."""
        if sys.platform != "win32":
            return None
        try:
            import ctypes
            from ctypes import wintypes
            hwnd = pygame.display.get_wm_info().get("window")
            if not hwnd:
                return None
            hmon = ctypes.windll.user32.MonitorFromWindow(hwnd, 2)        # MONITOR_DEFAULTTONEAREST

            class MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                            ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]
            mi = MONITORINFO()
            mi.cbSize = ctypes.sizeof(MONITORINFO)
            if not ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                return None
            r = mi.rcMonitor
            return int(r.left), int(r.top), int(r.right - r.left), int(r.bottom - r.top)
        except Exception:                                                # noqa: BLE001
            return None

    def set_fullscreen(on: bool) -> None:
        """Switch between BORDERLESS FULLSCREEN WINDOW (a frameless window covering the CURRENT monitor exactly
        — no video-mode switch, so alt-tab is instant and the compositor stays smooth) and the remembered
        resizable window."""
        import os
        settings["fullscreen"] = on
        if on:
            if not view.get("borderless"):
                view["win_size"] = view["screen"].get_size()              # remember the window size for the way back
            rect = _win_monitor_rect()                                    # exact monitor rect (DPI/multi-mon safe)
            if rect is not None:
                x, y, w, h = rect
            else:
                try:
                    w, h = pygame.display.get_desktop_sizes()[0]
                except Exception:                                        # noqa: BLE001 — older pygame
                    info = pygame.display.Info()
                    w, h = info.current_w, info.current_h
                x, y = 0, 0
            os.environ["SDL_VIDEO_WINDOW_POS"] = f"{x},{y}"               # SDL reads this on (re)create
            view["screen"] = pygame.display.set_mode((w, h), pygame.NOFRAME)
            view["borderless"] = True
            if rect is not None:                                         # force exact monitor placement (belt +
                try:                                                     # braces: SDL may reuse the window and
                    import ctypes                                        # skip the pos env; the surface is (w,h)
                    hwnd = pygame.display.get_wm_info().get("window")    # so a same-size move is safe)
                    if hwnd:
                        ctypes.windll.user32.SetWindowPos(hwnd, 0, x, y, w, h, 0x0014)   # NOZORDER|NOACTIVATE
                except Exception:                                        # noqa: BLE001
                    pass
        else:
            os.environ.pop("SDL_VIDEO_WINDOW_POS", None)
            wsz = view.get("win_size", (320 * args.scale, 200 * args.scale))
            view["screen"] = pygame.display.set_mode(wsz, pygame.RESIZABLE)
            view["borderless"] = False
            rect = _win_monitor_rect()                                   # SDL keeps the window at the fullscreen
            if rect is not None:                                         # origin (a tiny window in the corner) and
                mx, my, mw, mh = rect                                    # may not repaint the frame -> re-centre it
                cx, cy = mx + (mw - wsz[0]) // 2, my + (mh - wsz[1]) // 2   # on the monitor + force the frame back
                try:
                    import ctypes
                    hwnd = pygame.display.get_wm_info().get("window")
                    if hwnd:                                             # SWP_FRAMECHANGED|NOZORDER|NOACTIVATE
                        ctypes.windll.user32.SetWindowPos(hwnd, 0, cx, cy, wsz[0], wsz[1], 0x0034)
                except Exception:                                        # noqa: BLE001
                    pass
        ref["display_hz"] = detect_display_hz()                          # the window may now be on another monitor
        save_settings()

    if settings["fullscreen"]:                                            # persisted preference -> apply at boot
        set_fullscreen(True)

    _WIDE_MAX = 128                                       # widescreen margin cap (px/side): 576px total, ~2.9:1

    def wide_margin() -> int:
        """The widescreen margin (px each side) for the CURRENT window: just enough extra width for the
        window's aspect ratio at the game's 200px frame height (square pixels, like the 4:3 present), capped.
        0 when the Widescreen setting is off or the window is 16:10/4:3 or narrower."""
        if not settings["widescreen"]:
            return 0
        sw, sh = view["screen"].get_size()
        if sh <= 0:
            return 0
        return min(_WIDE_MAX, max(0, (round(200 * sw / sh) - 320 + 1) // 2))

    # Levels that must render in the plain 4:3 pipeline. LEVEL A (id 0x09) is the gorilla boss: (a) its alternate
    # faces (calm / roaring) sit in the tilemap columns just right of the 320 window, so widescreen margins reveal
    # them; and (b) its fight only plays right faithfully (verified: it works with widescreen OFF). Gameplay on
    # these levels uses the faithful stream path (see `enhance_ok`); any enhanced compose that still runs at level
    # start (the curtain reveal) is forced to margin 0 by `_margin_for` so the boss-face tiles never bleed in.
    _WS_EXCLUDE_LEVELS = {0x09}

    def _margin_for(state) -> int:
        """wide_margin(), but forced to 0 on levels that can't be widescreened (LEVEL A boss-face tiles)."""
        if state.data[DS + 0x2D8A] in _WS_EXCLUDE_LEVELS:
            return 0
        return wide_margin()
    # ENTER-CODE (password screen): host hex key -> DOS make code. The game maps the DOS make code to a hex char
    # via its own [0xB068] table, so we must feed the make code of the PHYSICAL key position — like the original,
    # which reads raw scancodes. Key by SDL physical scancode (ev.scancode), NOT the keysym (ev.key): the keysym is
    # LAYOUT-dependent, so on a non-US keyboard (e.g. Czech, where the number row is ě š č …) the digit keysyms
    # never match and the code can't be typed. The physical position is layout-independent.
    _SDL_HEX = {30: 0x02, 31: 0x03, 32: 0x04, 33: 0x05, 34: 0x06, 35: 0x07, 36: 0x08, 37: 0x09, 38: 0x0A,   # 1..9
                39: 0x0B,                                                                                    # 0
                4: 0x1E, 5: 0x30, 6: 0x2E, 7: 0x20, 8: 0x12, 9: 0x21,                                        # A..F
                89: 0x02, 90: 0x03, 91: 0x04, 92: 0x05, 93: 0x06, 94: 0x07, 95: 0x08, 96: 0x09, 97: 0x0A,    # KP1..9
                98: 0x0B}                                                                                    # KP0
    _KEYSYM_HEX = {pygame.K_0: 0x0B, pygame.K_1: 0x02, pygame.K_2: 0x03, pygame.K_3: 0x04, pygame.K_4: 0x05,
                   pygame.K_5: 0x06, pygame.K_6: 0x07, pygame.K_7: 0x08, pygame.K_8: 0x09, pygame.K_9: 0x0A,
                   pygame.K_a: 0x1E, pygame.K_b: 0x30, pygame.K_c: 0x2E, pygame.K_d: 0x20, pygame.K_e: 0x12,
                   pygame.K_f: 0x21}

    def save_screenshot():
        """F12: save the current frame (the clean game image, no letterbox) as a timestamped PNG under
        <game-root>/screenshots/."""
        rgb = ref.get("last")
        if rgb is None:
            return
        import time as _t
        out = Path(gr) / "screenshots"
        try:
            out.mkdir(exist_ok=True)
            arr = np.asarray(rgb, np.uint8)
            surf = pygame.Surface((arr.shape[1], arr.shape[0]))
            pygame.surfarray.blit_array(surf, arr.swapaxes(0, 1))
            path = out / f"pre2_{_t.strftime('%Y%m%d_%H%M%S')}.png"
            pygame.image.save(surf, str(path))
            print(f"screenshot saved: {path}")
        except Exception as e:                                            # noqa: BLE001 — read-only dir / no PIL
            print(f"(screenshot failed: {type(e).__name__}: {e})")

    def pump():
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT or (ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE):
                ref["running"] = False
            elif ev.type == pygame.VIDEORESIZE and not settings["fullscreen"]:   # drag-resize (fullscreen: SDL
                view["screen"] = pygame.display.set_mode((max(160, ev.w), max(100, ev.h)), pygame.RESIZABLE)
            elif (ev.type == pygame.KEYDOWN and ev.key == pygame.K_RETURN
                  and ev.mod & pygame.KMOD_ALT):                   # Alt+Enter = fullscreen toggle (the classic)
                set_fullscreen(not settings["fullscreen"])
            elif ev.type == pygame.KEYDOWN and ev.key == pygame.K_F10:
                ref["menu_request"] = True
            elif ev.type == pygame.KEYDOWN and ev.key == pygame.K_F12:
                save_screenshot()
            elif ev.type == pygame.KEYDOWN:                        # latch the hex make code typed THIS frame
                sc = _SDL_HEX.get(getattr(ev, "scancode", -1)) or _KEYSYM_HEX.get(ev.key)   # physical, keysym fallback
                if sc:
                    ref["last_scan"] = sc
        if ref["menu_request"]:
            ref["menu_request"] = False
            menu_modal()                                          # EVERY loop pumps -> the F10 menu opens anywhere
            #   (late-bound: defined below in main(); no loop runs before it exists)

    _bf = {"src_size": None, "src": None, "geom": None, "sub": None}

    def blit_frame(rgb):
        """Scale + letterbox one game frame onto the window (no flip) — shared by present() and the menu.

        Allocation-free steady state (the 240Hz path): a persistent source surface filled via blit_array,
        scaled DIRECTLY into a cached subsurface of the window at the letterbox rect — no per-frame Surface
        allocations, no extra blit, and the black letterbox is filled only when the geometry changes
        (profiled: the old make_surface + allocating transform.scale + blit was ~3.7ms/frame at 1440p; this
        path is ~1.4ms)."""
        arr = np.asarray(rgb, np.uint8)
        fh, fw = arr.shape[:2]                                    # the frame's OWN size (320x200, or 640x480 for
        screen = view["screen"]                                   # the 12h creators screen) — fit aspect-correct
        sw, sh = screen.get_size()
        f = min(sw / fw, sh / fh)                                 # fit THIS frame, PRESERVING aspect ratio
        if settings["integer_scale"] and f >= 1.0:
            f = float(int(f))                                     # crisp pixel-exact multiples (menu toggle)
        tw, th = max(1, int(fw * f)), max(1, int(fh * f))
        x0, y0 = (sw - tw) // 2, (sh - th) // 2
        if _bf["src_size"] != (fw, fh):
            _bf["src"] = pygame.Surface((fw, fh))
            _bf["src_size"] = (fw, fh)
        pygame.surfarray.blit_array(_bf["src"], arr.swapaxes(0, 1))
        geom = (id(screen), sw, sh, tw, th, x0, y0)
        if _bf["geom"] != geom:                                   # resize / new frame size -> re-letterbox once
            screen.fill((0, 0, 0))
            _bf["sub"] = screen.subsurface((x0, y0, tw, th))
            _bf["geom"] = geom
        pygame.transform.scale(_bf["src"], (tw, th), _bf["sub"])
        return screen

    _hud = {"font": None, "t0": 0.0, "ticks0": 0, "text": ""}
    _FRAME_CAPS = [0, 60, 120, 144, 240, -1]              # Display(auto) -> fixed caps -> Uncapped

    def present_hz() -> float:
        """The interpolated-presentation rate from the Frame-cap setting (0=detected display Hz)."""
        cap = settings["frame_cap"]
        return ref["display_hz"] if cap == 0 else (0.0 if cap < 0 else float(cap))

    def _ui_scale() -> float:
        """The F10-overlay UI scale, so it stays a readable PHYSICAL size on hi-DPI / 4K / fullscreen windows
        (the window is now DPI-aware = real pixels). "auto" = proportional to the window height (clamped);
        else the user's fixed percentage (100/150/200/300)."""
        os_scale = settings.get("overlay_scale", "auto")
        if os_scale != "auto":
            try:
                return max(0.5, float(os_scale) / 100.0)
            except (TypeError, ValueError):
                pass
        h = view["screen"].get_size()[1]
        return max(1.0, min(3.5, h / 600.0))

    def pace(fps: float) -> None:
        """Frame pacing: pygame's Clock.tick sleeps in ~ms grains (SDL_Delay), which lands ~60fps when asked
        for 240 — use the busy-wait variant for high rates (precise), plain tick otherwise, none for uncapped."""
        if fps <= 0:
            clock.tick()                                   # uncapped (still updates get_fps)
        elif fps > 90:
            clock.tick_busy_loop(fps)
        else:
            clock.tick(fps)

    def present(rgb, fps, caption=None):
        screen = blit_frame(rgb)
        if settings["fps_overlay"]:
            import time as _time
            us = _ui_scale()                                     # scale the readout with the UI (hi-DPI / 4K)
            if _hud["font"] is None or _hud.get("font_scale") != us:
                from overlay_menu import _load_font              # the same clean UI face as the F10 menu
                _hud["font"] = _load_font(pygame, max(9, int(round(13 * us))), False)
                _hud["font_scale"] = us
                _hud["surf"] = None
            font = _hud["font"]
            now = _time.perf_counter()
            if now - _hud["t0"] >= 0.5:                          # MEASURED rates over a rolling half-second:
                tps = (ref["tick_count"] - _hud["ticks0"]) / (now - _hud["t0"])   # real game ticks/sec (the
                _hud["t0"], _hud["ticks0"] = now, ref["tick_count"]               # proof enhancements never
                _hud["text"] = (f"{clock.get_fps():3.0f} fps  {tps:5.1f} tps"     # touch the tick cadence)
                                if tps > 0 else f"{clock.get_fps():3.0f} fps")
                _hud["surf"] = None                              # re-render the cached text surface
            surf = _hud.get("surf")
            if surf is None:                                     # cache: render text (and its backing box)
                text = font.render(_hud["text"], True, (190, 210, 190))          # once per 0.5s, not per frame
                px, py = int(round(5 * us)), int(round(3 * us))
                surf = pygame.Surface((text.get_width() + 2 * px, text.get_height() + 2 * py))
                surf.fill((10, 12, 14))                          # opaque black box: readable over the game AND
                surf.blit(text, (px, py))                        # self-erasing over the letterbox (no ghosting,
                _hud["surf"] = surf                              #  which the fill-once letterbox left behind)
            screen.blit(surf, (int(round(8 * us)), int(round(14 * us))))
        pygame.display.flip()
        pace(fps)
        if caption:
            pygame.display.set_caption(caption)
        ref["last"] = rgb

    def dump_gap_snapshot(state, msg: str) -> str | None:
        """Write the CURRENT native state as a repro snapshot the workbench loads directly:
        ``<dir>/memory_1mb.bin`` (the full 1.25 MB image — ``--snapshot <dir>`` re-seeds from it, and every
        probe/oracle does ``NativeGameState(bytearray(read_bytes()))``) + ``state.json`` (the gap message +
        the key game state for triage). Frozen exe -> next to the game data (discoverable); repo -> artifacts/."""
        import datetime
        import json
        try:
            base = Path(gr) if getattr(sys, "frozen", False) else ROOT / "artifacts"
            out = base / f"native_gap_{datetime.datetime.now():%Y%m%d_%H%M%S}"
            out.mkdir(parents=True, exist_ok=True)
            (out / "memory_1mb.bin").write_bytes(bytes(state.data))
            d = state.data
            (out / "state.json").write_text(json.dumps({
                "kind": "native_gap",
                "error": msg,
                "level_0x2d8a": d[DS + 0x2D8A],
                "lives_0x27d8": d[DS + 0x27D8],
                "frame_0x6bd5": d[DS + 0x6BD5] | (d[DS + 0x6BD6] << 8),
                "player_x": d[DS + 0x4F1C] | (d[DS + 0x4F1D] << 8),
                "player_y": d[DS + 0x4F1E] | (d[DS + 0x4F1F] << 8),
                "scale_0x6be2": d[DS + 0x6BE2] | (d[DS + 0x6BE3] << 8),
            }, indent=1))
            print(f"  gap snapshot written: {out}")
            print(f"  repro: python scripts/play_native.py --snapshot \"{out}\"")
            return str(out)
        except Exception as e:                                  # noqa: BLE001 — never mask the original gap
            print(f"  (gap snapshot failed: {type(e).__name__}: {e})")
            return None

    def hold_last(msg, state=None):
        """An unrecovered gap (or a finished run): dump a repro snapshot (when the game state is passed),
        print once, hold the last frame until the user quits."""
        if state is not None:
            dump_gap_snapshot(state, msg)
        print(f"  {msg}")
        pygame.display.set_caption(f"PRE2 VM-less — {msg[:80]}")
        while ref["running"]:
            pump()
            if ref["last"] is not None:
                present(ref["last"], 30)
            else:
                clock.tick(30)

    def drive_input(state):
        """Write DC1's key table from the demo (if replaying) merged with live host keys, then advance the demo
        by one frame. Numpad + arrows = move, SPACE = fire/jump, 1/2 = mode-select. Shared by front-end + game."""
        if demo is not None:
            demo.step()
        k = pygame.key.get_pressed()
        held = set(demo.held) if demo is not None else set()
        if k[pygame.K_SPACE]:
            held.add(0x39)
        if k[pygame.K_UP] or k[pygame.K_KP8]:
            held.add(0x48)
        if k[pygame.K_DOWN] or k[pygame.K_KP2]:
            held.add(0x50)
        if k[pygame.K_RIGHT] or k[pygame.K_KP6]:
            held.add(0x4D)
        if k[pygame.K_LEFT] or k[pygame.K_KP4]:
            held.add(0x4B)
        if k[pygame.K_1] or k[pygame.K_KP1]:
            held.add(0x02)
        if k[pygame.K_2]:
            held.add(0x03)
        # The Ctrl+Alt+<letter> Easter-egg combos: Left-Ctrl (0x1D) + Left-Alt (0x38) + W (0x11, the 247B
        # dev-credits combo) / E (0x12, the 25C7 game-over creators-photo combo). Without these the combos are
        # only reachable from a recorded demo's raw scancodes, never from a live keyboard.
        for key, sc in ((pygame.K_LCTRL, 0x1D), (pygame.K_LALT, 0x38), (pygame.K_w, 0x11), (pygame.K_e, 0x12),
                        (pygame.K_F1, 0x3B), (pygame.K_F2, 0x3C)):   # F1 = lose a life, F2 = abort->game over
            if k[key]:
                held.add(sc)
        for sc in set(DemoInput.STD) | held | {0x1D, 0x38, 0x11, 0x12, 0x3B, 0x3C}:
            set_key(state, sc, sc in held)
        # ENTER-CODE: drive the [0x2874] scancode latch DC1's 99BE reads from the hex key typed THIS frame (0 if
        # none). A per-frame latch (not a persistent queue) is deliberate: the '1'/'2' the player presses to REACH
        # the password screen must NOT leak into the code (they are hex chars too) — writing 0 every idle frame
        # clears any menu keystroke before the screen reads it. The password accumulator maps it via [0xB068].
        latch = ref["last_scan"]
        if state.data[(DS + 0x2879) & 0xFFFFF] == 1 and held:   # ATTRACT demo: ANY key press ends it (DC1 0DD6
            latch = latch or next(iter(held))                   # sets [0x6BE5] on a pending [0x2874]) -> back to menu
        state.data[(DS + 0x2874) & 0xFFFFF] = latch
        ref["last_scan"] = 0

    # ---- audio: the recovered ENHANCED player (VM-free), driven by the native frame's audio commands ----
    native_audio = None
    audio_post = None                                               # the sink's command inlet (menu toggles post here)
    try:
        from sdl_view import SdlEnhancedAudio
        from pre2.native.audio import NativeAudio
        audio_post = SdlEnhancedAudio(pygame, gr, {}).post
        native_audio = NativeAudio(audio_post, gr)
    except Exception as e:                                          # noqa: BLE001 — no audio device -> run silent
        print(f"  (audio disabled: {type(e).__name__}: {str(e)[:60]})")

    def _audio_apply_settings():
        """Push the music/sfx settings into the audio sink (SetMusicEnabled / SetSfxEnabled events)."""
        if audio_post is None:
            return
        from pre2.audio.events import SetMusicEnabled, SetSfxEnabled
        audio_post(SetMusicEnabled(enabled=bool(settings["music"])))
        audio_post(SetSfxEnabled(enabled=bool(settings["sfx"])))

    _audio_apply_settings()                                         # honour persisted settings from launch

    # --- smooth transitions (Experimental): render level-start/level-end effects full-width + smoothly over the
    #     composed WIDESCREEN frame, instead of streaming the faithful pillarboxed 320px transition frames. Pure
    #     presentation (reads state, writes nothing); the smooth projections live in pre2.enhanced.transitions. ---
    _tx = {"tex": None, "bg": None}

    def _smooth_active() -> bool:
        return settings["smooth_transitions"] and wide_margin() > 0

    def compose_wide_now(state, dos):
        """Compose the CURRENT gameplay state as a widescreen RGB frame (static, alpha=1). Returns
        (rgb, margin_left, efs) or None if this is not a gameplay frame (no object camera)."""
        from pre2.bridge.foreground_tiles import read_foreground_state
        from pre2.bridge.gameplay_effects import capture_gameplay_effects
        from pre2.enhanced.compositor import compose
        from pre2.enhanced.extract import extract_enhanced_frame
        from pre2.enhanced.native_background import TileTextureCache, _HudCache
        from pre2.enhanced.sprite_cache import SpriteTextureCache
        if _tx["tex"] is None:
            _tx["tex"] = SpriteTextureCache()
            _tx["bg"] = (TileTextureCache(), _HudCache())
        disp = state.data[DS + 0x2DD6] | (state.data[DS + 0x2DD7] << 8)
        fg = read_foreground_state(state)
        fg.page = disp & 0xFFFF
        fx = capture_gameplay_effects(state, particle_frame=getattr(state, "particle_capture_last", None),
                                      foreground_frame=fg)
        m = _margin_for(state)
        efs = extract_enhanced_frame(state, dos, game_root=gr, with_faithful=False, tex_cache=_tx["tex"],
                                     bg_cache=_tx["bg"], effects=fx, margin=m,
                                     wide_cull=settings["true_widescreen"], hud_align=settings["hud_align"],
                                     bg_mode=settings["widescreen_bg"])
        if efs is None:
            return None
        return compose(efs, None, 1.0), m, efs

    def _animate(state, duration, render, caption="PRE2 VM-less — transition"):
        """Present ``render(progress 0..1)`` at the DISPLAY rate for ``duration`` wall-clock seconds, so the
        effect is smooth AND frame-rate-INDEPENDENT (progress comes from the clock, not a fixed source step
        count). Ends exactly at progress 1.0. Pumps + polls audio each displayed frame."""
        from time import perf_counter
        t0 = perf_counter()
        while ref["running"]:
            p = (perf_counter() - t0) / duration if duration > 0 else 1.0
            present(render(min(1.0, p)), present_hz(), caption)
            pump()
            if native_audio is not None:
                native_audio.poll(state)
            if p >= 1.0 or not ref["running"]:
                break

    def _curtain_frame(rgb, p):
        """Center-out curtain of ``rgb`` at progress ``p`` — VIEWPORT only; the HUD strip is static chrome."""
        import numpy as np
        w = rgb.shape[1]
        cx = w // 2
        half = int(round(min(1.0, max(0.0, p)) * (w / 2.0)))
        fr = np.zeros_like(rgb)
        lo, hi = max(0, cx - half), min(w, cx + half)
        fr[:VIEWPORT_H, lo:hi] = rgb[:VIEWPORT_H, lo:hi]
        fr[VIEWPORT_H:] = rgb[VIEWPORT_H:]
        return fr

    def _present_smooth_reveal(state, dos):
        """The level-start CURTAIN reveal, smooth + full-width + FRAME-RATE-INDEPENDENT: compose the loaded
        level once, then wipe it in center-out over ``_REVEAL_S`` wall-clock seconds at the display rate.
        Returns True if it ran (smooth mode + a gameplay frame)."""
        wide = compose_wide_now(state, dos)
        if wide is None:
            return False
        rgb = wide[0]
        _animate(state, _REVEAL_S, lambda p: _curtain_frame(rgb, p), "PRE2 VM-less — level start")
        return True

    def reveal_level(state, dos):
        """Curtain the freshly-loaded level in (the VM's 3054 center-out level-start reveal) instead of it
        appearing instantly. Driven once at every level start (cold boot + between-levels)."""
        if _smooth_active() and _present_smooth_reveal(state, dos):
            return
        disp = state.data[DS + 0x2DD6] | (state.data[DS + 0x2DD7] << 8)
        for planes, page in native_level_reveal(state, dos, disp, game_root=gr):
            present(render_planar_rgb_from_planes(planes, page, dos.vga_palette), _TRANSITION_FPS,
                    "PRE2 VM-less — level start")
            pump()
            if native_audio is not None:
                native_audio.poll(state)
            if not ref["running"]:
                return

    def between_levels(state, dos):
        """The between-levels flow (the VM's 4F65 -> BRAVO tally -> CARTE world map -> next-level load): show the
        level-end TALLY (SCORE / LEVEL COMPLETED %), advance + load the next level (byte-exact), then drive the
        recovered CARTE scene with the 'you are here' marker at the NEW level (the VM advances [0x2D8A] before the
        carte too). The full 4CCB exit-anim cutscene (iris + player walk + food-throw count-up) is deferred; the
        tally TEXT is shown."""
        from pre2.native.audio import native_load_song
        from pre2.native.front_end import _native_carte
        from pre2.native.level_state import level_end_takes_tally, native_level_end

        # [asm 4C69] the level-end dispatch (level_end_takes_tally): a warp INTO a bonus level (4C8F) and a bonus
        # level's own end (4CC1) do `call 30C6` (the vertical close-curtain — the same visual as the cave
        # transition) + `jmp 4F65` (plain reload): NO exit anim, NO tally, NO carte.
        mode = state.data[DS + 0x6BE6]
        level = state.data[DS + 0x2D8A]
        if not level_end_takes_tally(mode, level):                 # [asm 4c93 / 4cc1] the curtain (cave-style) exit
            from pre2.native.audio import native_level_song_name
            from pre2.native.render import native_render, native_sync_render_state
            from pre2.native.runtime import _vfade_frame
            print("  level exit -> BONUS-warp curtain (30C6 close, no tally) -> next level")
            disp = state.data[DS + 0x2DD6] | (state.data[DS + 0x2DD7] << 8)
            native_sync_render_state(state)
            base_planes, base_page = native_render(state, dos, disp, game_root=gr, force_gameplay=True)
            for k in range(1, 10):                                 # [asm 30C6] 9-step vertical fade to black
                planes, page = _vfade_frame(base_planes, base_page, k)
                present(render_planar_rgb_from_planes(planes, page, dos.vga_palette), _TRANSITION_FPS,
                        "PRE2 VM-less — bonus warp")
                pump()
                if not ref["running"]:
                    return
            native_level_end(state, game_root=gr)                  # [asm 4f65] the warp-table level switch + load
            native_load_level_palette(state, dos)
            native_load_song(state, native_level_song_name(state), gr)
            reveal_level(state, dos)                               # 3054 center-out curtain into the new level
            return

        print("  level complete -> IRIS close -> exit-anim (walk-in + food-throw score count-up + walk-off) -> carte")
        disp = state.data[DS + 0x2DD6] | (state.data[DS + 0x2DD7] << 8)
        smooth = compose_wide_now(state, dos) if _smooth_active() else None
        if smooth is not None:
            # SMOOTH iris: drain native_iris_close for its STATE work (reading the recovered iris centre off the
            # first frame), then present-time animate a true circle 0xE6->0 over the frozen wide frame at the
            # display rate (frame-rate-independent), the player held on top.
            from pre2.bridge.render_state import read_renderer_state
            from pre2.enhanced.compositor import _blit
            from pre2.enhanced.transitions import apply_iris
            rgb, m, efs = smooth
            players = [i for i in efs.sprites if i.handle == ("player",)]
            center = None
            for planes, page in native_iris_close(state, dos, disp, game_root=gr):
                if center is None:
                    iris = read_renderer_state(state, dos).iris
                    if iris is not None:                           # centre: screen COL = center_y (+margin),
                        center = (iris.center_y + m, iris.center_x)   # ROW = center_x (axes swapped)
                pump()
                if native_audio is not None:
                    native_audio.poll(state)
                if not ref["running"]:
                    return
            if center is None:
                center = (rgb.shape[1] // 2, _VFADE_MID)

            def _iris_fr(p):
                fr = rgb.copy()
                r = 0xE6 * (1.0 - p)
                if r > 0:
                    apply_iris(fr, r, center[0], center[1])
                else:
                    fr[:] = 0
                for inst in players:
                    _blit(fr, inst.rgba, inst.screen_x + inst.tex_off_x, inst.screen_y + inst.tex_off_y)
                return fr
            _animate(state, _IRIS_S, _iris_fr, "PRE2 VM-less — level complete")
        else:
            for planes, page in native_iris_close(state, dos, disp, game_root=gr):   # 316F circle-close (faithful)
                present(render_planar_rgb_from_planes(planes, page, dos.vga_palette), _FRONT_END_FPS,
                        "PRE2 VM-less — level complete")
                pump()
                if native_audio is not None:
                    native_audio.poll(state)
                if not ref["running"]:
                    return
        try:
            native_load_song(state, "BRAVO.TRK", gr)               # the tally jingle
        except Exception:                                          # noqa: BLE001 — no audio -> silent tally
            pass
        disp = state.data[DS + 0x2DD6] | (state.data[DS + 0x2DD7] << 8)
        for planes, page in native_exit_anim(state, dos, disp, game_root=gr):   # walk-in + food + count-up + walk-off
            present(render_planar_rgb_from_planes(planes, page, dos.vga_palette), _TRANSITION_FPS,
                    "PRE2 VM-less — LEVEL COMPLETED")   # a main-loop (~23Hz) animation, NOT a 70Hz retrace scene
            pump()
            drive_input(state)
            if native_audio is not None:
                native_audio.poll(state)
            if not ref["running"]:
                return
        native_level_end(state, game_root=gr)
        for scene in _native_carte(state, dos, gr):                # fire (press after release) advances
            present(front_end_scene_to_rgb(scene), _FRONT_END_FPS, "PRE2 VM-less — world map")
            pump()
            drive_input(state)
            if native_audio is not None:
                native_audio.poll(state)                           # CARTE.TRK
            if not ref["running"]:
                return
        native_load_level_palette(state, dos)                      # restore the level palette after the carte DAC
        from pre2.native.audio import native_level_song_name
        native_load_song(state, native_level_song_name(state), gr)  # [asm 01B7] carte song -> the level song
        reveal_level(state, dos)                                    # 3054 center-out curtain into the next level

    def game_over_restart(state, dos):
        """[asm 5063 -> main 011C] The real game-over flow: the GAME OVER scene (9B23 — the GAMEOVER.SQZ
        diorama with the bouncing letters, the crying tableau + circling birds, BOULA.TRK, until fire or the
        ~9 s timeout, then the DAC fade), then main re-enters the front-end at the press-1/2 MENU (8e45) ->
        mode-select map -> carte -> the LEVEL loader with the FRESH-start block (lives reset). Recovered:
        native_gameover_scene (setup+tick byte-exact vs the ASM, 60-frame lockstep) + native_menu_flow (the
        same generator the cold boot runs from the menu on)."""
        from pre2.native.audio import native_load_song
        from pre2.native.front_end import native_creators_screen, native_menu_flow
        from pre2.native.gameover_scene import native_gameover_scene
        from pre2.native.player import ecombo_confirmed
        print("  GAME OVER -> the 9B23 scene -> menu -> carte -> restart")
        pump(); drive_input(state)                                  # refresh the key flags at the game-over moment
        if ecombo_confirmed(state):                                # [asm 506C: call 25C7] Ctrl+Alt+E held at game-over
            print("  Ctrl+Alt+E -> the creators photo (25C7 -> 25F6)")
            for scene in native_creators_screen(state, gr):        # the same mode-12h HOLLY DAY photo as THE END
                present(front_end_scene_to_rgb(scene), _FRONT_END_FPS, "PRE2 VM-less — creators")
                pump(); drive_input(state)
                if native_audio is not None:
                    native_audio.poll(state)
                if not ref["running"]:
                    return
        native_load_song(state, "BOULA.TRK", gr)                   # [asm 5063: 02CC ax=0x11] the game-over song
        for planes, page in native_gameover_scene(state, dos, gr):  # [asm 9B23] the scene (fire/timeout exits)
            # each scene frame is one 44FB present = 3 retraces (the ASM busy-waits 3 vsyncs), so it displays at
            # _FRONT_END_FPS/3 (~23Hz), NOT the per-retrace front-end rate — else the whole scene runs 3x too fast.
            present(render_planar_rgb_from_planes(planes, page, dos.vga_palette), _FRONT_END_FPS / 3,
                    "PRE2 VM-less — GAME OVER")
            pump(); drive_input(state)
            if native_audio is not None:
                native_audio.poll(state)                           # BOULA.TRK
            if not ref["running"]:
                return
        for scene in native_menu_flow(state, dos, gr):             # [main 011C] menu -> map -> carte -> loader
            fps = args.fps if state.data[DS + 0x2879] == 1 else _FRONT_END_FPS
            present(front_end_scene_to_rgb(scene), fps, "PRE2 VM-less — restart")
            pump(); drive_input(state)
            if native_audio is not None:
                native_audio.poll(state)
            if not ref["running"]:
                return
        native_load_level_palette(state, dos)                      # the level palette after the carte DAC
        reveal_level(state, dos)                                    # 3054 center-out curtain into the level

    def the_end_restart(state, dos):
        """[asm 5034 -> main 0x12f] THE END: the player cleared the final level 0xE ([0x6be5]==0xFF). Show the
        THEEND.SQZ screen (FINAL.TRK, fade-in, wait-for-fire, fade-out), then re-enter the front-end MENU (like
        the game-over restart) -> map -> carte -> the LEVEL loader = level 1 started again."""
        from pre2.native.audio import native_load_song
        from pre2.native.front_end import native_menu_flow, native_the_end
        print("  THE END -> THEEND.SQZ screen -> menu -> restart at level 1")
        try:
            native_load_song(state, "FINAL.TRK", gr)               # [asm 5034 region] the ending song
        except Exception:                                          # noqa: BLE001 — no audio -> silent ending
            pass
        for scene in native_the_end(state, gr):                    # [asm 5034] the THE END screen (fire exits)
            present(front_end_scene_to_rgb(scene), _FRONT_END_FPS, "PRE2 VM-less — THE END")
            pump(); drive_input(state)
            if native_audio is not None:
                native_audio.poll(state)                           # FINAL.TRK
            if not ref["running"]:
                return
        for sel in (0x6BE4, 0x6BE5, 0x6BE6):                       # clear the death/end selectors before the menu
            state.data[DS + sel] = 0
        for scene in native_menu_flow(state, dos, gr):             # [main 0x12f] menu -> map -> carte -> loader (L1)
            fps = args.fps if state.data[DS + 0x2879] == 1 else _FRONT_END_FPS
            present(front_end_scene_to_rgb(scene), fps, "PRE2 VM-less — restart")
            pump(); drive_input(state)
            if native_audio is not None:
                native_audio.poll(state)
            if not ref["running"]:
                return
        native_load_level_palette(state, dos)
        reveal_level(state, dos)

    def show_cheat_credits(state, dos):
        """[asm 247B->2505] The dev-credits cheat combo (Ctrl+Alt+W/Z, no other key). Show the OLDIES-style
        developer-credits screen over black, hold for fire (0BBE), restore the level palette (0BA0), and RESUME
        the same level (the combo is a pure overlay — gameplay state is untouched)."""
        from pre2.bridge.oldies_scene import build_credits_scene
        from pre2.native.front_end import WAIT_PRESS, native_scene_wait
        from pre2.native.render import native_load_dac_palette
        from pre2.native.front_end import FrontEndScene
        from pre2.recovered.scene import MODE_PLANAR
        print("  CHEAT COMBO -> developer credits (247B); press fire to resume")
        native_load_dac_palette(state, dos, 0x287E)                # [asm 0b92] the OLDIES green/yellow palette
        disp = state.data[DS + 0x2DD6] | (state.data[DS + 0x2DD7] << 8)
        phase = WAIT_PRESS
        while ref["running"]:
            planes, _ = build_credits_scene(state, page=disp)      # [asm 2505] the dev-name text over black
            scene = FrontEndScene(MODE_PLANAR, palette=tuple(dos.vga_palette),
                                  planes=tuple(bytes(p) for p in planes), page=disp)
            present(front_end_scene_to_rgb(scene), _FRONT_END_FPS, "PRE2 VM-less — developer credits")
            pump(); drive_input(state)
            phase, done = native_scene_wait(state, phase)          # [asm 0bbe] fire press -> release
            if done:
                break
        native_load_level_palette(state, dos)                      # [asm 0ba0] restore the level palette; resume

    def _p_edge():
        """A rising edge on the P key (scancode 0x19). pump() must have run this frame first."""
        p = pygame.key.get_pressed()[pygame.K_p]
        edge = p and not ref["p_prev"]
        ref["p_prev"] = p
        return edge

    def pause_check(state):
        """[asm 6294] The P-key PAUSE: press P to freeze, press P again to resume. 6294 is a pure busy-wait on the
        P-held flag [0x280D] with NO gameplay-state writes, so native excludes it from the frame model and it lives
        here as a presentation freeze — hold the last frame + keep the music alive (interrupts stay live during the
        original's spin, so music kept playing) until the next P edge."""
        if not _p_edge():
            return
        pygame.display.set_caption("PRE2 VM-less gameplay — PAUSED (P resumes)")
        while ref["running"]:
            pump()
            if ref["last"] is not None:
                present(ref["last"], _FRONT_END_FPS)
            else:
                clock.tick(_FRONT_END_FPS)
            if native_audio is not None:
                native_audio.poll(state)                           # music continues while frozen (as in the original)
            if _p_edge():
                break

    # ---- the F10 overlay menu (modal; visual style from pre2_editor) --------------------------------------
    from overlay_menu import OverlayMenu

    def _menu_tabs():
        """The tab/item data (re-evaluated each frame so values render live). Closures edit `settings` +
        `ref` — HOST/presentation state only; the Develop tab (cheats) exists only under --debug."""
        def onoff(k):
            return "On" if settings[k] else "Off"

        def toggle(k, apply=None):
            def act():
                settings[k] = not settings[k]
                if apply:
                    apply()
                save_settings()
            return act

        cap = settings["frame_cap"]
        cap_label = (f"Display ({ref['display_hz']:.0f} Hz)" if cap == 0
                     else "Uncapped" if cap < 0 else f"{cap} Hz")

        def adj_cap(d):
            caps = _FRAME_CAPS
            i = caps.index(cap) if cap in caps else 0
            settings["frame_cap"] = caps[(i + d) % len(caps)]
            save_settings()

        _HUD_ALIGNS = ["left", "center", "right"]

        def adj_hud(d):
            i = _HUD_ALIGNS.index(settings["hud_align"]) if settings["hud_align"] in _HUD_ALIGNS else 1
            settings["hud_align"] = _HUD_ALIGNS[(i + d) % len(_HUD_ALIGNS)]
            save_settings()

        _MENU_SCALES = ["auto", 100, 150, 200, 300]

        def adj_menu_scale(d):
            cur = settings.get("overlay_scale", "auto")
            i = _MENU_SCALES.index(cur) if cur in _MENU_SCALES else 0
            settings["overlay_scale"] = _MENU_SCALES[(i + d) % len(_MENU_SCALES)]
            save_settings()

        _sc = settings.get("overlay_scale", "auto")
        menu_scale_label = "Auto" if _sc == "auto" else f"{_sc}%"

        _BG_MODES = ["stretch", "mirror", "black"]     # widescreen backdrop margin fill

        def adj_bg(d):
            i = _BG_MODES.index(settings["widescreen_bg"]) if settings["widescreen_bg"] in _BG_MODES else 0
            settings["widescreen_bg"] = _BG_MODES[(i + d) % len(_BG_MODES)]
            save_settings()

        view_tab = [
            {"label": "Interpolation", "value": onoff("interpolation"), "activate": toggle("interpolation")},
            {"label": "Frame cap", "value": cap_label, "adjust": adj_cap},
            {"label": "Widescreen", "value": onoff("widescreen"), "activate": toggle("widescreen")},
            {"label": "Widescreen backdrop", "value": settings["widescreen_bg"].capitalize(),
             "adjust": adj_bg},
            {"label": "HUD position", "value": settings["hud_align"].capitalize(),
             "adjust": adj_hud},
            {"label": "Fullscreen", "value": onoff("fullscreen"),
             "activate": lambda: set_fullscreen(not settings["fullscreen"])},
            {"label": "Smooth transitions", "value": onoff("smooth_transitions"),
             "activate": toggle("smooth_transitions")},
            {"label": "Integer scaling", "value": onoff("integer_scale"), "activate": toggle("integer_scale")},
            {"label": "FPS overlay", "value": onoff("fps_overlay"), "activate": toggle("fps_overlay")},
            {"label": "Menu scale", "value": menu_scale_label, "adjust": adj_menu_scale},
        ]
        audio_tab = [
            {"label": "Music", "value": onoff("music"), "activate": toggle("music", _audio_apply_settings)},
            {"label": "Sound effects", "value": onoff("sfx"), "activate": toggle("sfx", _audio_apply_settings)},
        ]
        experimental_tab = [
            {"label": "True widescreen", "value": onoff("true_widescreen"), "activate": toggle("true_widescreen")},
            {"label": "", "info": True},
            {"label": "Draws objects in the widescreen margins — not accurate", "info": True},
            {"label": "to the original. Game logic is unaffected.", "info": True},
        ]
        tabs = [("View", view_tab), ("Audio", audio_tab), ("Experimental", experimental_tab)]
        if args.debug:
            lvl = ref.get("menu_level", 0)

            def adj_level(d):
                ref["menu_level"] = (lvl + d) % 0x10       # playable ids 0x00..0x0F (LEVEL1..9, A..G); id 0x10
                #   is NOT a bootable level — the per-level DGROUP tables end at 0x0F (LEVELH/I.SQZ are the
                #   ending-credits scenery the finale loads by its own path, not via the level selector)

            def go_level():
                ref["switch_level"] = ref.get("menu_level", 0)
                menu.open = False

            lvl_name = str(lvl + 1) if lvl < 9 else chr(ord("A") + lvl - 9)   # 0..8 -> 1..9, 9..0xF -> A..G
            tabs.append(("Develop", [
                {"label": "God mode", "value": onoff("god"), "activate": toggle("god")},
                {"label": "Level", "value": f"LEVEL{lvl_name}", "adjust": adj_level, "activate": go_level},
                {"label": "Restart level", "value": "reload current", "activate": lambda: (
                    ref.__setitem__("switch_level", "restart"), setattr(menu, "open", False))},
            ]))
        return tabs

    menu = OverlayMenu(pygame, _menu_tabs)

    def menu_modal():
        """The modal F10 overlay, openable from ANY loop (every scene pumps): the game/scene is frozen while
        open and every key event is routed to the menu — nothing it consumes can reach the game's input
        cells, so demo determinism and the oracle chain are structurally untouched. Music keeps playing
        (the SDL sink streams the current song autonomously; no polling needed while frozen)."""
        menu.open = True
        while ref["running"] and menu.open:
            for ev in pygame.event.get():
                if ev.type == pygame.QUIT:
                    ref["running"] = False
                elif ev.type == pygame.VIDEORESIZE and not settings["fullscreen"]:
                    view["screen"] = pygame.display.set_mode((max(160, ev.w), max(100, ev.h)), pygame.RESIZABLE)
                elif (ev.type == pygame.KEYDOWN and ev.key == pygame.K_RETURN
                      and ev.mod & pygame.KMOD_ALT):
                    set_fullscreen(not settings["fullscreen"])
                elif ev.type == pygame.KEYDOWN:
                    menu.handle_keydown(ev)
                elif ev.type in (pygame.MOUSEMOTION, pygame.MOUSEWHEEL, pygame.MOUSEBUTTONDOWN):
                    menu.handle_mouse(ev)
            if ref["last"] is not None:
                screen = blit_frame(ref["last"])
            else:                                                 # menu before the first frame -> over black
                screen = view["screen"]
                screen.fill((0, 0, 0))
            menu.draw(screen, _ui_scale())
            pygame.display.flip()
            clock.tick(60)

    def gameplay_loop(state, dos):
        """Run the recovered gameplay VM-less: host input -> native_frame_step -> present, until a gap.

        Structure: one loop iteration = one game TICK (native_frame_step; a transition yields several
        presentation frames within its tick). ``present_tick_frame`` is the single seam where every rendered
        gameplay frame reaches the screen — the (future) interpolation replaces exactly this: hold prev+cur
        FrameSnapshots and present lerped frames at ref["display_hz"] instead of one faithful frame per tick.
        The TICK cadence itself never changes with enhancements — only what is shown between ticks."""
        print("Gameplay — SPACE = fire/jump, arrows/numpad = move, P = pause, ESC = quit. (VM-less native gameplay)")
        from time import perf_counter
        from pre2.bridge.foreground_tiles import read_foreground_state
        from pre2.bridge.gameplay_effects import capture_gameplay_effects
        from pre2.enhanced.compositor import compose
        from pre2.enhanced.extract import extract_enhanced_frame
        from pre2.enhanced.sprite_cache import SpriteTextureCache
        from pre2.gaps import Pre2CheatCredits, Pre2GameComplete, Pre2GameOverTransition, Pre2LevelEndTransition
        n = 0
        tick_dt = 1.0 / TICK_HZ
        enh = {"tex": SpriteTextureCache(), "prev": None, "next_tick": None,   # the prev+cur snapshot pair
               "last_cur": None}   # smooth-tx: the last wide gameplay/bounce frame = the fade base

        def present_tick_frame(planes, page):
            """Present one faithful gameplay frame, paced at the tick rate (the enhancement seam)."""
            nonlocal n
            rgb = render_planar_rgb_from_planes(planes, page, dos.vga_palette)
            n += 1
            present(rgb, args.fps, None if n % 20 else f"PRE2 VM-less gameplay — {clock.get_fps():.0f} fps")

        def present_interpolated(planes, page):
            """The INTERPOLATION presentation for a steady (single-frame) tick: extract the tick's snapshot,
            then present compose(cur, prev, alpha) frames at the DISPLAY rate until the next tick is due
            (a fixed-timestep accumulator — the tick cadence is enforced by wall-clock due times, never
            changed by how many frames are shown between ticks). Pure presentation: reads the state, writes
            nothing — the endpoints are parity-proven pixel-equal to the faithful frames (alpha=1 gate)."""
            # The EFFECTS bundle, mirroring native_render's own construction (same sources; the tick's
            # native_render already consumed the one-shots, so use the *_last stashes it leaves): point
            # particles (spider threads/sparkles), foreground tiles (z-order OVER sprites), fireflies, snow.
            nonlocal n
            disp2 = state.data[DS + 0x2DD6] | (state.data[DS + 0x2DD7] << 8)
            fg = read_foreground_state(state)
            fg.page = disp2 & 0xFFFF
            fx = capture_gameplay_effects(state, particle_frame=getattr(state, "particle_capture_last", None),
                                          foreground_frame=fg)
            # Re-apply the one-frame OPAQUE flash bits (cleared during the tick) around the snapshot, exactly
            # like native_render — restore right after so carried-forward state stays byte-exact.
            flash = getattr(state, "flash_slots_last", None)
            saved = None
            if flash:
                saved = [(off, state.data[DS + off + 5]) for off in flash]
                for off in flash:
                    state.data[DS + off + 5] |= 0x40
            try:
                cur = extract_enhanced_frame(state, dos, game_root=gr, with_faithful=False,
                                             tex_cache=enh["tex"], effects=fx, margin=_margin_for(state),
                                             wide_cull=settings["true_widescreen"], hud_align=settings["hud_align"],
                                             bg_mode=settings["widescreen_bg"])
            finally:
                if saved is not None:
                    for off, v in saved:
                        state.data[DS + off + 5] = v
            if cur is None:                                     # no snapshot -> one faithful frame
                present_tick_frame(planes, page)
                enh["prev"] = None
                enh["next_tick"] = None
                return
            enh["last_cur"] = cur                                # smooth-tx fade base = the last wide gameplay frame
            if not settings["interpolation"]:                   # WIDESCREEN without interpolation: one composed
                n += 1                                          #   (wide) frame per tick, faithful pacing
                present(compose(cur, None, 1.0), args.fps,
                        None if n % 20 else f"PRE2 VM-less gameplay — {clock.get_fps():.0f} fps")
                enh["prev"] = cur                               # keep the pair warm for a live interp toggle-on
                enh["next_tick"] = None
                return
            if (enh["prev"] is not None
                    and enh["prev"].background_rgb.shape != cur.background_rgb.shape):
                enh["prev"] = None                              # widescreen width changed (resize/toggle) -> snap
            now = perf_counter()
            if enh["next_tick"] is None or enh["next_tick"] < now - 0.25:   # (re)sync after start/pause/menu
                enh["next_tick"] = now
            if enh["prev"] is None:                             # no pair yet -> the composed frame, unlerped
                present(compose(cur, None, 1.0), args.fps)
                enh["prev"] = cur
                enh["next_tick"] += tick_dt
                return
            n += 1
            while True:                                         # >=1 frame per tick, then up to the due time
                now = perf_counter()
                alpha = min(1.0, max(0.0, 1.0 - (enh["next_tick"] - now) * TICK_HZ))
                present(compose(cur, enh["prev"], alpha), present_hz(),
                        None if n % 20 else f"PRE2 VM-less gameplay — {clock.get_fps():.0f} fps")
                if perf_counter() >= enh["next_tick"]:
                    break
                pump()                                          # stay responsive between ticks (F10/resize/quit;
                if not ref["running"]:                          #  a menu stall resyncs via the 0.25s clause)
                    break
            enh["prev"] = cur
            enh["next_tick"] += tick_dt

        def present_smooth_tx_run(state, dos, run):
            """Present a SMOOTH FULL-WIDTH, FRAME-RATE-INDEPENDENT cave/death transition. ``run`` is the whole
            transition (already drained from native_frame_step_tagged, so ``state`` is now at the destination):
            its ``tx`` phases decide fade -> black -> curtain. The fade dims the last wide gameplay/bounce frame
            (enh.last_cur); the reveal wipes in the freshly-composed wide destination room. Each phase runs by
            wall clock at the display rate (not the source step count)."""
            from pre2.enhanced.compositor import compose
            from pre2.enhanced.transitions import apply_vfade
            enh["prev"] = enh["next_tick"] = None               # a transition breaks the interpolation pair
            kinds = {t[3][0] for t in run if t[3] is not None}
            old = compose(enh["last_cur"], None, 1.0) if enh["last_cur"] is not None else None
            neww = compose_wide_now(state, dos)                 # state is at the arrival/checkpoint now
            new = neww[0] if neww is not None else None
            if "fade" in kinds and old is not None:
                _animate(state, _FADE_S, lambda p: apply_vfade(
                    old.copy(), int(_VFADE_MID * p), int(2 * _VFADE_MID - _VFADE_MID * p)))
            if "black" in kinds:
                base = old if old is not None else new
                if base is not None:
                    def _black(p):
                        fr = base.copy(); fr[:VIEWPORT_H] = 0
                        return fr
                    _animate(state, _CAVE_BLACK_S, _black)
            if "reveal" in kinds:
                base = new if new is not None else old
                if base is not None:
                    _animate(state, _REVEAL_S, lambda p: _curtain_frame(base, p))

        while ref["running"]:
            pump()
            pause_check(state)                                     # [asm 6294] P freezes here until P resumes
            if ref["switch_level"] is not None:                    # Develop tab: jump/restart (a --debug cheat)
                lvl = ref["switch_level"]
                ref["switch_level"] = None
                lvl = state.data[DS + 0x2D8A] if lvl == "restart" else lvl
                print(f"menu: switching to level id {lvl:#04x}")
                state = native_cold_boot(gr, level=lvl)
                native_load_level_palette(state, dos)
                from pre2.native.audio import native_level_song_name, native_load_song
                native_load_song(state, native_level_song_name(state), gr)   # the NEW level's song (else the
                #                                                              previous level's music keeps playing)
                reveal_level(state, dos)
            if args.debug and settings["god"]:                     # Develop tab: keep the energy topped up
                state.data[DS + 0x27D6] = 3                        # [asm 52a8] full hearts, refreshed pre-tick
            drive_input(state)
            disp = state.data[DS + 0x2DD6] | (state.data[DS + 0x2DD7] << 8)

            def stream_frame(planes, page):
                """One faithful frame of a (possibly multi-frame) tick — today's path, per-frame audio."""
                present_tick_frame(planes, page)
                pump()
                if native_audio is not None:
                    native_audio.poll(state)                   # PER FRAME: a transition (death fly-off) yields
                    #   dozens of frames in ONE step — its queued sfx (the death SCREAM at the bounce start,
                    #   509d/50a6) must sound AT that frame, not after the whole animation
                return ref["running"]

            try:
                # Per-frame tag decides interpolation: an ``interpolatable`` frame (the normal tick AND every
                # death-BOUNCE frame — object motion) lerps; a transition frame (cave curtain, death fade +
                # checkpoint curtain, scene) streams 1:1 and breaks the lerp pair. So the parabolic death arc
                # smooths out (each of its 60 frames is a lerp tick) while wipes stay faithful.
                # Some levels ALWAYS render faithfully (no enhanced compose / interpolation / smooth transitions):
                # LEVEL A (0x09) is the gorilla boss whose fight only plays right in the plain 4:3 pipeline
                # (widescreen also bleeds its boss-face tiles). Matches the verified-working non-widescreen case.
                enhance_ok = state.data[DS + 0x2D8A] not in _WS_EXCLUDE_LEVELS
                _it = iter(native_frame_step_tagged(state, dos, disp, game_root=gr))
                for planes, page, interp_ok, tx in _it:
                    # A SMOOTH transition run: drain the whole transition (state advances to the destination),
                    # then present it present-time full-width (fade -> black -> curtain). The preceding bounce
                    # frames (interp) were already presented + stashed as the fade base (enh.last_cur).
                    if enhance_ok and _smooth_active() and tx is not None:
                        run = [(planes, page, interp_ok, tx)]
                        run.extend(_it)
                        present_smooth_tx_run(state, dos, run)
                        break
                    # Enhanced present path when interpolation OR widescreen is on (widescreen needs the composed
                    # wide frame even unlerped); a transition frame streams faithful 1:1 (smooth off) and breaks
                    # the lerp pair.
                    if enhance_ok and (settings["interpolation"] or settings["widescreen"]) and interp_ok:
                        present_interpolated(planes, page)
                        pump()
                        if native_audio is not None:
                            native_audio.poll(state)
                        if not ref["running"]:
                            break
                    else:
                        enh["prev"] = enh["next_tick"] = None   # transition (or interp off) -> break the lerp pair
                        if not stream_frame(planes, page):
                            break
                ref["tick_count"] += 1                         # one native_frame_step drive == one game tick
            except Pre2LevelEndTransition:
                between_levels(state, dos)                          # tally/carte flow, then the next level
            except Pre2CheatCredits:
                show_cheat_credits(state, dos)                     # dev-credits overlay, then resume this level
            except Pre2GameComplete:
                the_end_restart(state, dos)                        # THE END screen; then menu -> restart at level 1
            except Pre2GameOverTransition:
                game_over_restart(state, dos)                      # death-bounce shown; restart at level 1
            except Exception as e:                                  # noqa: BLE001 — hold on an unrecovered gap
                hold_last(f"gameplay gap: {type(e).__name__}: {str(e)[:80]}", state)
                return
            if native_audio is not None:
                native_audio.poll(state)

    if args.play_demo:
        tick_path = Path(args.play_demo) / "game_tick_demo.bin"
        if tick_path.exists():
            # ---- DETERMINISTIC tick replay: seed + per-tick keys + per-tick digest from the VM oracle ----
            # (produced by scripts/verify_native_tick_demo.py; keyed to GAME TICKS, so it replays identically
            # in every mode. Live keys are IGNORED during the replay — determinism first; ESC still quits.)
            from pre2.gaps import Pre2GameComplete, Pre2GameOverTransition, Pre2LevelEndTransition
            from pre2.native.game_tick_demo import GameTickDemo, _inject, gameplay_digest
            gtd = GameTickDemo.load(tick_path)
            print(f"tick replay: {gtd.n_ticks} game ticks (deterministic; digest-checked vs the VM oracle)")
            state = NativeGameState(bytearray(gtd.seed))           # the VM's memory at the first gameplay tick
            dos = NativeVGA()
            native_load_level_palette(state, dos)
            div = None
            i = 0
            while ref["running"] and i < gtd.n_ticks:
                pump()
                _inject(state, gtd.keys[i], gtd.idle[i] if i < len(gtd.idle) else None)
                disp = state.data[DS + 0x2DD6] | (state.data[DS + 0x2DD7] << 8)
                try:
                    for planes, page in native_frame_step(state, dos, disp, game_root=gr):
                        present(render_planar_rgb_from_planes(planes, page, dos.vga_palette), args.fps,
                                f"PRE2 VM-less — tick replay {i}/{gtd.n_ticks}" if i % 20 == 0 else None)
                        pump()
                        if native_audio is not None:
                            native_audio.poll(state)           # per frame (death fly-off sfx timing)
                        if not ref["running"]:
                            break
                except Pre2LevelEndTransition:
                    print(f"  tick replay: LEVEL END at tick {i} — the compare ends here; continuing live")
                    between_levels(state, dos)
                    break
                except Pre2GameOverTransition:
                    print(f"  tick replay: GAME OVER at tick {i} — the compare ends here; restarting live")
                    game_over_restart(state, dos)
                    break
                except Pre2GameComplete:
                    print(f"  tick replay: THE END at tick {i} — the game is finished")
                    the_end_restart(state, dos)
                    break
                except Exception as e:                             # noqa: BLE001
                    hold_last(f"tick replay gap at tick {i}: {type(e).__name__}: {str(e)[:70]}", state)
                    pygame.quit()
                    return 0
                if div is None and gameplay_digest(state.data[DS:DS + 0x10000]) != gtd.digests[i]:
                    div = i
                    print(f"  tick replay DIVERGENCE at tick {i} (gameplay digest mismatch) — continuing")
                if native_audio is not None:
                    native_audio.poll(state)
                i += 1
            if div is None and i:
                print(f"  tick replay: {i} ticks reproduced byte-identically (digest matched every tick)")
            if ref["running"]:
                gameplay_loop(state, dos)                          # hand over to live play
            pygame.quit()
            return 0
        print(f"(no {tick_path.name} in the demo — approximate input replay; run "
              f"scripts/verify_native_tick_demo.py {args.play_demo} once to make it deterministic)")

    if args.from_level is not None:
        # ---- DEBUG path: jump straight into a level for gameplay testing (no front-end) ----
        print(f"--from-level {args.from_level}: booting LEVEL{args.from_level + 1} directly (VM-less, no front-end)...")
        state = native_cold_boot(gr, level=args.from_level)
        dos = NativeVGA()
        native_load_level_palette(state, dos)
        from pre2.native.audio import native_load_song, native_level_song_name
        native_load_song(state, native_level_song_name(state), gr)  # start the level music (the front-end normally does this at 01B7)
        reveal_level(state, dos)                                    # 3054 center-out curtain into the level
        gameplay_loop(state, dos)
        pygame.quit()
        return 0

    if args.snapshot is not None:
        # ---- DEBUG path: seed gameplay from a savestate's raw memory (VM-less), then run native forward ----
        state = NativeGameState(bytearray((Path(args.snapshot) / "memory_1mb.bin").read_bytes()))
        lvl = state.data[DS + 0x2D8A]
        frame_ctr = state.data[DS + 0x6BD5] | (state.data[DS + 0x6BD6] << 8)
        player_zero = not any(state.data[DS + 0x4F1C:DS + 0x4F20])   # X+Y both zero
        if frame_ctr == 0 and player_zero:
            # A savestate taken DURING a level-load / transition (F12 mid-curtain): the gameplay DGROUP is not
            # populated yet (player/objects/camera/frame-counter all zero, ip parked in the loader's retrace
            # wait). Native has no "resume a half-loaded level" path, so seed a CLEAN LEVEL{lvl+1} instead.
            print(f"--snapshot: DGROUP is PRE-GAMEPLAY (level {lvl + 1} mid-load — player/objects/frame-counter "
                  f"all zero). Native can't resume a half-loaded level; booting LEVEL{lvl + 1} fresh instead.")
            state = native_cold_boot(gr, level=lvl)
        else:
            print(f"--snapshot: seeding LEVEL{lvl + 1} gameplay from the savestate (VM-less)...")
        init_keyboard_input(state)
        dos = NativeVGA()
        native_load_level_palette(state, dos)
        from pre2.native.audio import native_load_song, native_level_song_name
        native_load_song(state, native_level_song_name(state), gr)  # start the level music (the front-end normally does this at 01B7)
        reveal_level(state, dos)                                    # 3054 center-out curtain into the level
        gameplay_loop(state, dos)
        pygame.quit()
        return 0

    # ---- the real cold start: OLDIES -> titles -> menu -> map -> level, all VM-less ----
    print("Cold boot from the OLDIES screen (VM-less). SPACE to advance, ESC to quit...")
    state = NativeGameState(build_boot_memory())      # the boot CONSTANTS (no EXE, no boot image)
    init_keyboard_input(state)                                     # the boot joystick-detect outcome (DC1 input)
    dos = NativeVGA()
    reached_gameplay = False
    try:
        for scene in native_front_end(state, dos, 0, game_root=gr):
            # front-end scenes are per-retrace (70Hz), but GAME-TICK-paced scenes run at the game rate
            # (args.fps ~23Hz) or they play ~3x too fast: the ATTRACT demo ([0x2879]=1, GAMEPLAY) and the
            # attract TITLE ANIMATION (scene.game_paced — the VM presents it via 44FB's 3-retrace 1C6F wait).
            fps = args.fps if (state.data[DS + 0x2879] == 1 or scene.game_paced) else _FRONT_END_FPS
            present(front_end_scene_to_rgb(scene), fps, "PRE2 VM-less — cold boot (front-end)")
            pump()
            # the OLDIES scene-wait (0bbe) reads fire; the mode-select toggles BEGINNER<->EXPERT on UP/DOWN and
            # the carte pans on the arrows; '1'/'2' start / password. drive_input feeds all of these (demo + live).
            drive_input(state)
            if native_audio is not None:
                native_audio.poll(state)                           # front-end music (PRESENTA title song, menu, carte)
            if not ref["running"]:
                break
        reached_gameplay = ref["running"]                          # the generator finished -> a level started
    except Pre2HybridGap as e:
        hold_last(f"front-end reached a not-yet-recovered gap: {str(e)[:110]}", state)
    except Exception as e:                                         # noqa: BLE001
        hold_last(f"front-end error: {type(e).__name__}: {str(e)[:90]}", state)

    if reached_gameplay and ref["running"]:
        native_load_level_palette(state, dos)
        reveal_level(state, dos)                                    # 3054 center-out curtain into the level
        gameplay_loop(state, dos)

    pygame.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
