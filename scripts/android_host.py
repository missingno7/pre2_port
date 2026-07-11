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

import os
import sys


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

    def __init__(self, *, android: bool) -> None:
        from touch_overlay import TouchOverlay
        from pre2.native.touch import MenuGestures
        self._overlay = TouchOverlay(mouse_emulation=not android)   # gameplay joystick + JUMP/BASH buttons
        self._menu = MenuGestures()                                 # front-end whole-screen tap / swipe
        self.enabled = True
        self.jump_edge = False        # a gameplay JUMP tap edge (for the responsive-controls jump buffer)
        self.menu_fire = False        # front-end: a tap completed this poll -> inject fire (0x39)
        self.menu_arrow = 0           # front-end: a swipe crossed this poll -> latch this arrow scancode (0x48/0x50)
        self._prev_frontend = None    # last context, to detect a switch and reset stale finger ownership

    # -- event intake / per-poll tick ------------------------------------------------------------------
    def handle_event(self, ev, size) -> None:
        """Feed one pygame event (FINGER*/mouse). Both the gameplay resolver and the menu gestures read the
        overlay's finger set, so a single event stream serves both contexts."""
        self._overlay.handle_event(ev, size)

    def tick(self, size, *, frontend: bool) -> None:
        """Resolve the active touches for the current context. Call once per input poll (in ``pump()``)."""
        if self._prev_frontend is not None and frontend != self._prev_frontend:
            self.reset()                                            # clear stale finger ownership across a switch
        self._prev_frontend = frontend
        self._overlay.tick(size)                                    # always track fingers + the gameplay flags
        if frontend:
            self.menu_fire, self.menu_arrow = self._menu.update(self._overlay.active_points(), size)
            self.jump_edge = False                                  # menus don't jump
        else:
            self.menu_fire, self.menu_arrow = False, 0
            self.jump_edge = self._overlay.jump_edge

    # -- outputs ---------------------------------------------------------------------------------------
    def scancodes(self) -> set[int]:
        """The GAMEPLAY movement/fire scancodes (joystick dirs + JUMP 0x48 / BASH 0x39). Only meaningful when
        NOT in the front-end — the caller branches on ``frontend`` and uses :attr:`menu_fire` /
        :attr:`menu_arrow` there instead."""
        return self._overlay.scancodes()

    def draw(self, disp, *, frontend: bool) -> None:
        """Draw the on-screen controls — only in gameplay; the menus are driven by whole-screen gestures with
        nothing to draw."""
        if not frontend:
            self._overlay.draw(disp)

    def reset(self) -> None:
        """Clear all held finger state (call on focus loss / app pause, or a context switch)."""
        self._overlay.reset()
        self._menu.reset()
