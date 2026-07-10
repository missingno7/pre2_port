"""Pygame host for the virtual touch controls — event translation + rendering.

Bridges the pure resolver (``pre2.native.touch.TouchControls``) to pygame: it turns real ``FINGER*``
multitouch events (and, for desktop testing, a single mouse pointer) into the finger dict the resolver
wants, ticks the resolver once per input poll, exposes the resulting scancodes to ``drive_input``, and
paints the joystick + JUMP/BASH buttons onto a translucent window-size canvas.

Only this file (and the rest of ``scripts/``) touches pygame — ``pre2/`` stays backend-agnostic. The
painted overlay is cached and only re-rendered when the control state actually changes, so presenting it
on every interpolated frame is cheap.
"""
from __future__ import annotations

import pygame

from pre2.native.touch import TouchControls

_MOUSE_ID = "mouse"

# translucent skin (RGBA) — tuned to read over the bright game without hiding it
_RING = (235, 235, 245, 70)
_RING_EDGE = (255, 255, 255, 120)
_KNOB = (245, 245, 255, 120)
_KNOB_ACTIVE = (120, 210, 255, 190)
_BTN = (235, 235, 245, 70)
_BTN_EDGE = (255, 255, 255, 120)
_BTN_PRESSED = (120, 210, 255, 190)
_LABEL = (20, 25, 35, 220)


class TouchOverlay:
    def __init__(self, *, mouse_emulation: bool = True) -> None:
        self.controls = TouchControls()
        self.enabled = True
        self.mouse_emulation = mouse_emulation
        self._fingers: dict[object, tuple[float, float]] = {}
        self._mouse: tuple[float, float] | None = None
        self._saw_finger = False                 # a real touch disables mouse emulation (avoid double input)
        # last tick result (consumed by drive_input / present)
        from pre2.native.touch import Flags, RenderModel  # noqa: F401 — for type clarity only
        self.flags = self.controls.update({}, (1, 1))[0]
        self.render_model = None
        self.jump_edge = False
        # cached overlay surface
        self._surf = None
        self._sig = None
        self._font = None

    # -- event intake ---------------------------------------------------------------------------------
    def handle_event(self, ev, size: tuple[int, int]) -> None:
        """Feed one pygame event. ``size`` is the current window size (FINGER coords are normalised)."""
        w, h = size
        t = ev.type
        if t == pygame.FINGERDOWN:
            self._saw_finger = True
            self._mouse = None
            self._fingers[ev.finger_id] = (ev.x * w, ev.y * h)
        elif t == pygame.FINGERMOTION:
            self._fingers[ev.finger_id] = (ev.x * w, ev.y * h)
        elif t == pygame.FINGERUP:
            self._fingers.pop(ev.finger_id, None)
        elif self.mouse_emulation and not self._saw_finger:
            if t == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                self._mouse = (float(ev.pos[0]), float(ev.pos[1]))
            elif t == pygame.MOUSEMOTION and self._mouse is not None:
                self._mouse = (float(ev.pos[0]), float(ev.pos[1]))
            elif t == pygame.MOUSEBUTTONUP and ev.button == 1:
                self._mouse = None

    def _active_points(self) -> dict[object, tuple[float, float]]:
        pts = dict(self._fingers)
        if self._mouse is not None and not self._fingers:
            pts[_MOUSE_ID] = self._mouse
        return pts

    # -- per-poll tick --------------------------------------------------------------------------------
    def tick(self, size: tuple[int, int]) -> None:
        """Resolve the active touches into flags + a render model. Call once per input poll (in pump())."""
        self.flags, self.render_model, self.jump_edge = self.controls.update(self._active_points(), size)

    def scancodes(self) -> set[int]:
        return self.flags.scancodes()

    def reset(self) -> None:
        """Clear held state (e.g. on focus loss / app pause)."""
        self._fingers.clear()
        self._mouse = None
        self.controls.reset()

    # -- rendering ------------------------------------------------------------------------------------
    def _get_font(self, px: int):
        if self._font is None or self._font[0] != px:
            self._font = (px, pygame.font.Font(None, px))
        return self._font[1]

    def surface(self, size: tuple[int, int]):
        """A cached translucent SRCALPHA surface with the controls painted, sized to the window. Rebuilt
        only when the control state (or window size) changes."""
        rm = self.render_model
        if rm is None:
            return None
        sig = (size, rm.signature())
        if self._surf is not None and self._sig == sig:
            return self._surf
        lay = rm.layout
        surf = pygame.Surface(size, pygame.SRCALPHA)

        # joystick: ring + knob
        _circle(surf, _RING, rm.stick_base, lay.stick_radius, 0)
        _circle(surf, _RING_EDGE, rm.stick_base, lay.stick_radius, max(2, int(lay.unit * 0.006)))
        _circle(surf, _KNOB_ACTIVE if rm.stick_active else _KNOB, rm.knob, lay.knob_radius, 0)

        # two buttons
        self._button(surf, lay.jump_center, lay.button_radius, "JUMP", rm.jump_pressed, lay.unit)
        self._button(surf, lay.bash_center, lay.button_radius, "BASH", rm.bash_pressed, lay.unit)

        self._surf = surf
        self._sig = sig
        return surf

    def _button(self, surf, center, r, label, pressed, unit) -> None:
        _circle(surf, _BTN_PRESSED if pressed else _BTN, center, r, 0)
        _circle(surf, _BTN_EDGE, center, r, max(2, int(unit * 0.006)))
        font = self._get_font(max(12, int(r * 0.7)))
        text = font.render(label, True, _LABEL)
        surf.blit(text, text.get_rect(center=(int(center[0]), int(center[1]))))


def _circle(surf, color, center, r, width) -> None:
    pygame.draw.circle(surf, color, (int(center[0]), int(center[1])), max(1, int(r)), int(width))
