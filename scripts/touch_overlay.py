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
        # small control sprites (built once per window size), drawn as cheap quads each frame
        self._spr = None
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

    def _disc(self, r, fill, edge_col, edge, label=None):
        """A small SRCALPHA disc surface (fill + edge ring + optional centred label)."""
        d = int(2 * r) + 2 * edge + 2
        s = pygame.Surface((d, d), pygame.SRCALPHA)
        c = (d // 2, d // 2)
        pygame.draw.circle(s, fill, c, int(r), 0)
        pygame.draw.circle(s, edge_col, c, int(r), edge)
        if label:
            font = self._get_font(max(12, int(r * 0.7)))
            t = font.render(label, True, _LABEL)
            s.blit(t, t.get_rect(center=c))
        return s

    def _build_sprites(self, disp, size):
        """Build the control TEXTURES once (uploaded to the GPU a single time). The joystick ring, the knob
        (idle/active), and each button (idle/pressed) become small static sprites; drawing them each frame is
        then a handful of cheap quad draws at the current positions — no per-frame window-size upload."""
        from pre2.native.touch import layout_for
        lay = layout_for(size)
        edge = max(2, int(lay.unit * 0.006))
        rr = lay.stick_radius
        ring = self._disc(rr, _RING, _RING_EDGE, edge)                # full ring (UP registers only while BASH held)
        self._spr = {
            "size": size, "lay": lay,
            "ring": disp.make_sprite(ring),
            "knob": disp.make_sprite(self._disc(lay.knob_radius, _KNOB, _KNOB, 0)),
            "knob_a": disp.make_sprite(self._disc(lay.knob_radius, _KNOB_ACTIVE, _KNOB_ACTIVE, 0)),
            "jump": disp.make_sprite(self._disc(lay.button_radius, _BTN, _BTN_EDGE, edge, "JUMP")),
            "jump_p": disp.make_sprite(self._disc(lay.button_radius, _BTN_PRESSED, _BTN_EDGE, edge, "JUMP")),
            "bash": disp.make_sprite(self._disc(lay.button_radius, _BTN, _BTN_EDGE, edge, "BASH")),
            "bash_p": disp.make_sprite(self._disc(lay.button_radius, _BTN_PRESSED, _BTN_EDGE, edge, "BASH")),
        }

    def draw(self, disp) -> None:
        """Draw the controls as small static sprites at their current positions (cheap GPU quads, no upload).
        Replaces the old full-window ``surface()`` + ``draw_overlay`` which re-uploaded ~18 MB every frame the
        knob moved — the phone's #1 stall."""
        rm = self.render_model
        if rm is None or not self.enabled:
            return
        size = disp.get_size()
        if self._spr is None or self._spr["size"] != size:
            self._build_sprites(disp, size)
        spr = self._spr

        def at(sprite, cx, cy):
            _, w, h = sprite
            disp.draw_sprite(sprite, (cx - w / 2, cy - h / 2))

        lay = rm.layout
        if rm.stick_active:                               # HIDDEN until touched: the stick appears at the thumb
            at(spr["ring"], rm.stick_base[0], rm.stick_base[1])
            at(spr["knob_a"], rm.knob[0], rm.knob[1])
        at(spr["jump_p"] if rm.jump_pressed else spr["jump"], lay.jump_center[0], lay.jump_center[1])
        at(spr["bash_p"] if rm.bash_pressed else spr["bash"], lay.bash_center[0], lay.bash_center[1])
