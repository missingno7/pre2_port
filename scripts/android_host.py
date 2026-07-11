"""Android / touch integration for the native runtime — all the mobile-specific glue in ONE place, kept out
of ``play_native.py`` so the desktop path stays clean.

This module owns:

* **platform detection** (running under python-for-android) and the touch-first defaults;
* **forcing the presentation enhancements ON** for the touch build (there is no F10 menu on a phone, so a
  stale settings file must not silently disable widescreen / interpolation / etc.);
* the **panel-refresh probe** — SDL/pygame report 60 Hz on Android even on a 90/120 Hz panel, so read the
  real rate via jnius;
* :class:`TouchController` — the **context-aware on-screen input**: the virtual joystick + JUMP/BASH buttons
  DURING GAMEPLAY, and a whole-screen **tap = fire** / vertical **swipe = up/down arrow** for the front-end
  MENUS (far more phone-native than hunting for a tiny button on the title/mode-select screens).

The gameplay resolver (:class:`pre2.native.touch.TouchControls`) and the menu gesture math
(:class:`pre2.native.touch.MenuGestures`) are the pure, unit-tested pieces; this is only the pygame host
that drives them. pygame lives here (like all of ``scripts/``); ``pre2/`` stays backend-agnostic.

The module is named ``android_host`` (not ``android``) so it never shadows python-for-android's own
top-level ``android`` package on ``sys.path``.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def on_android() -> bool:
    """True when running under python-for-android — it sets ``ANDROID_ARGUMENT`` in the environment, and
    the CPython/p4a build also exposes ``sys.getandroidapilevel``."""
    return "ANDROID_ARGUMENT" in os.environ or hasattr(sys, "getandroidapilevel")


def resolve_touch_enabled(cli_value, *, android: bool) -> bool:
    """The touch-controls default: on Android they default ON (touch-first). ``--touch`` / ``--no-touch`` (a
    real ``bool`` from argparse) always wins; only the unset ``None`` falls back to the platform default."""
    return android if cli_value is None else bool(cli_value)


# The presentation enhancements the touch build forces ON, applied AFTER the persisted settings load (there
# is no on-device F10 menu, and a stale settings file must not silently turn them off).
_TOUCH_FORCED_SETTINGS = {
    "interpolation": True, "widescreen": True, "true_widescreen": True, "smooth_transitions": True,
    "stereo_sfx": True, "responsive_controls": True,
    "intro_skippable": True,   # a tap skips the long intro titles to the menu by default
}


def force_touch_settings(settings: dict) -> None:
    """Force the mobile presentation enhancements on, in place (see :data:`_TOUCH_FORCED_SETTINGS`)."""
    settings.update(_TOUCH_FORCED_SETTINGS)


def android_refresh_hz() -> float:
    """The panel's REAL refresh rate via jnius (SDL/pygame report 60 on Android even on a 90/120 Hz panel).
    Returns ``0.0`` when unavailable (not on Android, or jnius/display missing) so the caller falls back."""
    try:
        from jnius import autoclass                                  # the real panel rate (often 90/120 Hz)
        act = autoclass("org.kivy.android.PythonActivity").mActivity
        r = float(act.getWindowManager().getDefaultDisplay().getRefreshRate())
        return r if r > 1 else 0.0
    except Exception:                                                # noqa: BLE001 — jnius/display unavailable
        return 0.0


def hide_system_bars() -> None:
    """Put the app in IMMERSIVE-STICKY fullscreen — hide BOTH the Android status bar and the navigation bar so
    the game fills the whole panel (SDL's own fullscreen flag hides the status bar at best, not the nav bar).

    No-op off Android. The View.setSystemUiVisibility flags must be set on the UI thread (via p4a's
    ``android.runnable.run_on_ui_thread``); immersive-STICKY means a swipe only reveals the bars transiently and
    they re-hide themselves, so this needs re-applying only on resume (see the caller), not every frame.
    """
    try:
        from android.runnable import run_on_ui_thread                # p4a's UI-thread hop (its own `android` pkg)
    except Exception:                                                # noqa: BLE001 — not on Android / pkg absent
        return

    @run_on_ui_thread
    def _apply():
        try:
            from jnius import autoclass
            act = autoclass("org.kivy.android.PythonActivity").mActivity
            View = autoclass("android.view.View")
            decor = act.getWindow().getDecorView()
            decor.setSystemUiVisibility(                             # the pre-30 immersive-sticky flag set (still
                View.SYSTEM_UI_FLAG_LAYOUT_STABLE                    # honoured on API 30+, just deprecated); minSdk 26
                | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION               # hide the nav bar
                | View.SYSTEM_UI_FLAG_FULLSCREEN                    # hide the status bar
                | View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY)            # a swipe reveals them transiently, then re-hides
        except Exception:                                            # noqa: BLE001 — jnius/window unavailable
            pass

    try:
        _apply()
    except Exception:                                                # noqa: BLE001 — run_on_ui_thread scheduling failed
        pass


# ---------------------------------------------------------------------------------------------------
# Progress save — the furthest level reached on each path, so the mobile CONTINUE screen can offer a
# level to resume from (the graphical stand-in for the original ENTER-CODE password screen). This is
# host-side player convenience, NOT game state: it only records what the byte-exact runtime reached, and
# only ever WRITES the game's own [0x2D8A]/[0xB197] the same values a valid password would, so it can't
# affect accuracy. Levels are the 0-based [0x2D8A] index; the path is [0xB197] (0 beginner / 1 expert).
# ---------------------------------------------------------------------------------------------------
_PROGRESS_FILE = "pre2native_progress.json"
# The 10 real main levels are [0x2D8A] 0..9 (the password checkpoints the CONTINUE grid shows). BONUS / special
# levels carry HIGHER ids (>= 0x0A, e.g. the warp-table bonuses 0x0A..0x0E) yet are reachable FROM a main level —
# so they must NOT count toward "furthest reached", or entering one bonus would wrongly unlock the whole column.
_REAL_LEVELS = 0x0A


def progress_path(game_root: str) -> Path:
    return Path(game_root) / _PROGRESS_FILE


def load_progress(game_root: str) -> dict:
    """Load ``{"beginner": idx, "expert": idx}`` — the furthest 0-based level reached per path (``-1`` = none
    reached yet). Missing/unreadable file (first run) yields the empty default."""
    prog = {"beginner": -1, "expert": -1}
    try:
        data = json.loads(progress_path(game_root).read_text())
        for k in ("beginner", "expert"):
            v = data.get(k)
            if isinstance(v, int):
                prog[k] = max(-1, min(int(v), 0x3F))          # clamp to a sane range (defends a hand-edited file)
    except Exception:                                          # noqa: BLE001 — first run / unreadable
        pass
    return prog


def save_progress(game_root: str, prog: dict) -> None:
    try:
        progress_path(game_root).write_text(json.dumps({"beginner": prog.get("beginner", -1),
                                                         "expert": prog.get("expert", -1)}, indent=1))
    except Exception as e:                                     # noqa: BLE001 — read-only game dir
        print(f"(progress not saved: {e})")


def record_reached(prog: dict, level: int, expert: bool, game_root: str) -> bool:
    """Note that ``[0x2D8A]==level`` was reached on the beginner/expert path. Advances (and persists) the stored
    furthest only when it grows — so it is a cheap no-op to call every gameplay frame. Returns True if it grew.

    Bonus / special levels (id ``>= _REAL_LEVELS``) are IGNORED: they have higher ids than the main levels but are
    reached FROM one, so counting them would over-unlock the CONTINUE grid (see :data:`_REAL_LEVELS`)."""
    if not (0 <= level < _REAL_LEVELS):
        return False
    key = "expert" if expert else "beginner"
    if level > prog.get(key, -1):
        prog[key] = int(level)
        save_progress(game_root, prog)
        return True
    return False


class TouchController:
    """Context-aware on-screen touch input.

    * **Gameplay** — the virtual joystick + JUMP/BASH buttons (drawn, and resolved to movement/fire
      scancodes) via :class:`~touch_overlay.TouchOverlay`.
    * **Front-end menus** — the controls are HIDDEN and the whole screen is the input: a tap is fire, a
      vertical swipe is an up/down arrow (the mode-select toggle) via
      :class:`pre2.native.touch.MenuGestures`.

    The caller passes ``frontend`` (its ``ref['frontend']``) into :meth:`tick` / :meth:`draw` so this stays a
    pure function of that one source of truth; a context switch resets the in-flight finger state so a tap
    held across the boundary can't leak into gameplay (or vice-versa)."""

    _MENU_MOUSE = "mouse"          # the desktop mouse-as-one-finger id for the menu gestures

    def __init__(self, *, android: bool) -> None:
        from touch_overlay import TouchOverlay
        from android_menu import MenuButtons
        from pre2.native.touch import MenuGestures
        self._overlay = TouchOverlay(mouse_emulation=not android)   # gameplay joystick + JUMP/BASH buttons
        self._menu = MenuGestures()                                 # front-end whole-screen tap / swipe
        self._buttons = MenuButtons()                               # NEW GAME / CONTINUE on the press-1/2 screen
        self._mouse_emulation = not android                         # desktop: drive the menu gestures with the mouse
        self.enabled = True
        self.jump_edge = False        # a gameplay JUMP tap edge (for the responsive-controls jump buffer)
        self._prev_frontend = None    # last context, to detect a switch and reset stale finger ownership
        self._screen = ""             # the current front-end screen (drives button drawing + swipe gating)

    def set_screen(self, screen: str) -> None:
        """Tell the menu recogniser which front-end screen is showing, BEFORE the frame's events are fed. Only
        the mode-select screen wants swipe gestures; everywhere else a drag stays a tap (fire). Call once per
        frame in ``pump()`` (before ``handle_event``), since the swipe is classified on the touch-move edge."""
        self._screen = screen
        self._menu.swipe_enabled = (screen == "mode_select")

    def menu_button_at(self, pos, size) -> str | None:
        """Which press-1/2 front-door button ('new' / 'continue') a tap at ``pos`` hit, or None. Used by
        ``drive_input`` on the ``"menu"`` screen to route a tap to NEW GAME ('1') or the CONTINUE screen."""
        from android_menu import menu_button_at
        return menu_button_at(pos, size)

    # -- event intake / per-poll tick ------------------------------------------------------------------
    def handle_event(self, ev, size) -> None:
        """Feed one pygame event. It drives BOTH the gameplay overlay (finger tracking + joystick) and the
        front-end menu gestures — the menu recogniser is event-driven (not poll-driven) so a fast tap whose
        FINGERDOWN and FINGERUP land in the same frame still registers."""
        self._overlay.handle_event(ev, size)
        self._route_menu_event(ev, size)

    def _route_menu_event(self, ev, size) -> None:
        """Translate a pygame FINGER* / mouse event into a MenuGestures edge (on_down/on_move/on_up)."""
        import pygame
        w, h = size
        t = ev.type
        if t == pygame.FINGERDOWN:
            self._menu.on_down(ev.finger_id, (ev.x * w, ev.y * h))
        elif t == pygame.FINGERMOTION:
            self._menu.on_move(ev.finger_id, (ev.x * w, ev.y * h), size)
        elif t == pygame.FINGERUP:
            self._menu.on_up(ev.finger_id)
        elif self._mouse_emulation:                                 # desktop testing: the mouse is one finger
            if t == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                self._menu.on_down(self._MENU_MOUSE, (float(ev.pos[0]), float(ev.pos[1])))
            elif t == pygame.MOUSEMOTION:
                self._menu.on_move(self._MENU_MOUSE, (float(ev.pos[0]), float(ev.pos[1])), size)
            elif t == pygame.MOUSEBUTTONUP and ev.button == 1:
                self._menu.on_up(self._MENU_MOUSE)

    def tick(self, size, *, frontend: bool) -> None:
        """Resolve the active touches for the current context. Call once per input poll (in ``pump()``).

        Does NOT drain the menu gesture here: a single presented frame calls ``pump()`` (hence ``tick()``)
        MANY times — the interpolation / smooth-fade sub-frame loops in ``present_front_scene`` — so draining
        per tick would consume a tap in an internal pump and lose it before ``drive_input`` reads it. The
        gesture accumulates in ``MenuGestures`` and is drained once per frame by :meth:`consume_menu`."""
        if self._prev_frontend is not None and frontend != self._prev_frontend:
            self.reset()                                            # clear stale finger ownership across a switch
        self._prev_frontend = frontend
        self._overlay.tick(size)                                    # always track fingers + the gameplay flags
        self.jump_edge = self._overlay.jump_edge and not frontend   # menus don't jump

    def consume_menu(self) -> tuple[bool, int, object]:
        """Drain the front-end tap/swipe accumulated since the last call: ``(fire, arrow, tap_pos)``. Called ONCE
        per frame by ``drive_input`` — so it survives the several ``pump()``/``tick()`` calls one presented frame
        makes, instead of a sub-frame pump eating the tap (the 'menu barely responds to taps' bug). ``tap_pos`` is
        the window-pixel position of the tap (for menu-button hit-testing), or None when there was no tap."""
        fire, arrow = self._menu.poll()
        return fire, arrow, self._menu.tap_pos

    # -- outputs ---------------------------------------------------------------------------------------
    def scancodes(self) -> set[int]:
        """The GAMEPLAY movement/fire scancodes (joystick dirs + JUMP 0x48 / BASH 0x39). Only meaningful when
        NOT in the front-end — the caller branches on ``frontend`` and uses :meth:`consume_menu` there instead."""
        return self._overlay.scancodes()

    def draw(self, disp, *, frontend: bool) -> None:
        """Draw the on-screen controls. Gameplay: the joystick + JUMP/BASH. Front-end: nothing EXCEPT the
        press-1/2 ``"menu"`` screen, which shows the NEW GAME / CONTINUE buttons (every other menu is driven by
        whole-screen gestures with nothing to draw)."""
        if not frontend:
            self._overlay.draw(disp)
        elif self._screen == "menu":
            self._buttons.draw(disp, disp.get_size())

    def reset(self) -> None:
        """Clear all held finger state (call on focus loss / app pause, or a context switch)."""
        self._overlay.reset()
        self._menu.reset()
