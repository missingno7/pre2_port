"""Virtual touch controls — geometry + flag mapping for a phone host (pure, no pygame).

The mobile input layout the Android host draws and drives:

* **Left half — a virtual analog joystick.** Touch anywhere in the left zone to plant the stick base
  there (a *floating* stick — the base follows your thumb-down, not a fixed spot), then drag: the knob
  tracks the finger, clamped to the ring, and its vector becomes the four direction flags via a
  dead-zone + per-axis threshold (so cardinals and diagonals both read cleanly).
* **Right half — two round buttons.** ``JUMP`` sets the *up* flag (DOS scancode ``0x48``) and ``BASH``
  sets the *fire* flag (``0x39``) — the very flags ``pre2.native.input`` already feeds the FSM. BASH is
  the club attack; JUMP is the up-key jump.

This module owns only the *math*, and is deliberately free of pygame and game state: given the active
touch points (window-pixel coords, keyed by a stable finger id) and the window size, it resolves each
finger to a control and yields the five movement flags plus a small render model for the host to draw.
The host (``scripts/touch_overlay.py``) translates real pygame ``FINGER*`` / mouse events into the
finger dict, calls :meth:`TouchControls.update` once per input poll, feeds the flags to
``apply_input``/``set_key``, and paints the render model. Pure + stateful = unit-testable with no device.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from math import hypot
from typing import Hashable

# DOS scancodes — the SAME sources DC1 reads (kept in step with pre2.native.input). The host turns the
# resolved flags into these and writes them into the key table exactly as a keyboard ISR would.
SCAN_FIRE = 0x39           # BASH  -> fire flag  [0x27E8]
SCAN_UP = 0x48             # JUMP  -> up flag    [0x27EA]
SCAN_DOWN = 0x50           # stick down          [0x27EB]
SCAN_RIGHT = 0x4D          # stick right         [0x27EC]
SCAN_LEFT = 0x4B           # stick left          [0x27ED]


@dataclass(frozen=True)
class Layout:
    """Screen-relative placement of the controls, derived from the window size each frame (so it tracks
    resize / rotation). All coordinates are window pixels; ``unit`` = ``min(w, h)`` keeps the controls a
    consistent physical size across aspect ratios. Every fraction below is a tuning knob."""
    w: float
    h: float
    unit: float
    stick_rest: tuple[float, float]      # where the (inactive) stick is drawn at rest
    stick_radius: float                  # ring radius = max knob travel
    knob_radius: float
    left_zone_x: float                   # a touch with x <= this may grab the stick
    top_band: float                      # y < this is reserved (HUD / F10) — ignored by the controls
    jump_center: tuple[float, float]
    bash_center: tuple[float, float]
    button_radius: float


def layout_for(size: tuple[float, float]) -> Layout:
    """Compute the control layout for a ``(w, h)`` window. Landscape is assumed (the game is played
    sideways); the fractions still produce a usable portrait layout, just cramped."""
    w, h = float(size[0]), float(size[1])
    u = min(w, h)
    return Layout(
        w=w, h=h, unit=u,
        stick_rest=(0.17 * w, 0.72 * h),
        stick_radius=0.15 * u,
        knob_radius=0.07 * u,
        left_zone_x=0.48 * w,
        top_band=0.16 * h,
        jump_center=(0.75 * w, 0.60 * h),   # upper-left of the pair
        bash_center=(0.90 * w, 0.76 * h),   # lower-right, nearest the thumb rest
        button_radius=0.11 * u,
    )


@dataclass
class Flags:
    """The five movement flags the game consumes (one per DC1 input source)."""
    left: bool = False
    right: bool = False
    up: bool = False
    down: bool = False
    fire: bool = False

    def scancodes(self) -> set[int]:
        """The active flags as the DOS scancodes ``pre2.native.input.set_key`` expects."""
        out: set[int] = set()
        if self.left: out.add(SCAN_LEFT)
        if self.right: out.add(SCAN_RIGHT)
        if self.up: out.add(SCAN_UP)
        if self.down: out.add(SCAN_DOWN)
        if self.fire: out.add(SCAN_FIRE)
        return out

    def any(self) -> bool:
        return self.left or self.right or self.up or self.down or self.fire


@dataclass
class RenderModel:
    """What the host needs to paint one frame of the controls."""
    layout: Layout
    stick_base: tuple[float, float]
    knob: tuple[float, float]
    stick_active: bool
    jump_pressed: bool
    bash_pressed: bool

    def signature(self) -> tuple:
        """A cheap value the host can diff to skip re-drawing an unchanged overlay."""
        return (
            round(self.layout.w), round(self.layout.h),
            round(self.stick_base[0]), round(self.stick_base[1]),
            round(self.knob[0]), round(self.knob[1]),
            self.stick_active, self.jump_pressed, self.bash_pressed,
        )


def _in_circle(x: float, y: float, center: tuple[float, float], r: float) -> bool:
    return hypot(x - center[0], y - center[1]) <= r


@dataclass
class TouchControls:
    """Stateful resolver: maps the active fingers to the five flags, remembering which finger owns which
    control across frames (so a finger that slides off a button still counts as held until it lifts, and
    the floating stick keeps its planted base). Construct once; call :meth:`update` each input poll."""

    DEADZONE: float = 0.28     # fraction of stick_radius the knob must leave before any direction reads
    DIR_THRESH: float = 0.34   # per-axis fraction past which that axis' flag sets (diagonals when both do)

    _stick_finger: Hashable | None = field(default=None, init=False)
    _stick_base: tuple[float, float] | None = field(default=None, init=False)
    _jump_finger: Hashable | None = field(default=None, init=False)
    _bash_finger: Hashable | None = field(default=None, init=False)
    _jump_prev: bool = field(default=False, init=False)

    def reset(self) -> None:
        """Drop all finger ownership (call on focus loss / pause so nothing sticks 'held')."""
        self._stick_finger = self._stick_base = None
        self._jump_finger = self._bash_finger = None
        self._jump_prev = False

    def update(self, fingers: dict[Hashable, tuple[float, float]],
               size: tuple[float, float]) -> tuple[Flags, RenderModel, bool]:
        """Resolve one poll. ``fingers`` maps a stable finger id -> its current ``(x, y)`` window-pixel
        position. Returns ``(flags, render_model, jump_edge)`` where ``jump_edge`` is True only on the
        frame JUMP first goes down (for the responsive-controls jump buffer)."""
        lay = layout_for(size)
        ids = set(fingers)

        # Release ownership for any finger that has lifted since last poll.
        if self._stick_finger not in ids:
            self._stick_finger = self._stick_base = None
        if self._jump_finger not in ids:
            self._jump_finger = None
        if self._bash_finger not in ids:
            self._bash_finger = None

        # Assign each new (unowned) finger to a control. Buttons win over the stick when overlapping.
        owned = {self._stick_finger, self._jump_finger, self._bash_finger}
        for fid, (x, y) in fingers.items():
            if fid in owned:
                continue
            if self._jump_finger is None and _in_circle(x, y, lay.jump_center, lay.button_radius):
                self._jump_finger = fid
            elif self._bash_finger is None and _in_circle(x, y, lay.bash_center, lay.button_radius):
                self._bash_finger = fid
            elif self._stick_finger is None and x <= lay.left_zone_x and y >= lay.top_band:
                self._stick_finger = fid
                self._stick_base = (x, y)                 # floating stick: plant the base under the thumb
            else:
                continue
            owned.add(fid)

        jump = self._jump_finger is not None
        bash = self._bash_finger is not None

        flags = Flags()
        base = lay.stick_rest
        knob = base
        stick_active = False
        if self._stick_finger is not None and self._stick_base is not None:
            base = self._stick_base
            fx, fy = fingers[self._stick_finger]
            dx, dy = fx - base[0], fy - base[1]
            dist = hypot(dx, dy)
            r = lay.stick_radius
            if dist > r and dist > 0.0:                    # clamp the knob to the ring (full circle)
                dx *= r / dist
                dy *= r / dist
                dist = r
            knob = (base[0] + dx, base[1] + dy)
            stick_active = True
            if dist >= self.DEADZONE * r:
                k = self.DIR_THRESH * r
                if dx <= -k:
                    flags.left = True
                elif dx >= k:
                    flags.right = True
                if dy >= k:
                    flags.down = True
                elif dy <= -k and bash:                    # stick UP only registers WHILE BASH is held (an upward
                    flags.up = True                        # bash) — so walking up-diagonal never triggers a jump

        if jump:
            flags.up = True                                # JUMP button == up flag (also reachable via stick-up)
        if bash:
            flags.fire = True                              # BASH button == fire flag (the club attack)

        jump_edge = jump and not self._jump_prev
        self._jump_prev = jump

        rm = RenderModel(layout=lay, stick_base=base, knob=knob, stick_active=stick_active,
                         jump_pressed=jump, bash_pressed=bash)
        return flags, rm, jump_edge
