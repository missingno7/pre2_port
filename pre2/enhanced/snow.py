"""ENHANCED LEVELG snow — widescreen-aware, frame-rate-independent presentation of the recovered
``scroll_script_snow`` (1030:396A) effect.

The faithful plot: ``wind`` ([0x6BF6]) flakes, each a 1px white dot advancing ``+0x4F`` page bytes per game
tick — net **(−8 px, +2 rows)** per tick (79 = 2·40 − 1: two rows down, one byte left) — the first half of the
flakes doubled one row down, wrapped inside the 175-row viewport, plotted at 4 dithered sub-byte positions.
This field reproduces that LOOK continuously: the same density per 320 px of width (scaled to the wide frame),
the same velocity in px/second (per-tick × the 70/3 Hz game rate), the same white dots + pair structure and a
±jitter echoing the sub-byte dither — advanced by wall-clock ``dt`` at the display rate, across the whole
widescreen width. PURE PRESENTATION: it reads only the wind magnitude; the gameplay tick still runs the real
``scroll_script_snow`` every tick (its shared-rng advance is byte-exact-critical) — the enhanced path simply
draws this field instead of the tick's 320px plot list.
"""
from __future__ import annotations

import numpy as np

_TICK_HZ = 70.0 / 3.0
VX = -8.0 * _TICK_HZ         # px/s left  (the −1 byte per tick, wind-blown slant)
VY = 2.0 * _TICK_HZ          # px/s down  (the +2 rows per tick)
_VIEW_H = 175                # the faithful wrap window (0x1B58 bytes = rows 0..174; HUD rows excluded)
_SEED = 0x6BF6               # fixed -> deterministic field per session (pure presentation)


class SnowField:
    """A persistent presentation snow field. ``draw(frame, wind, dt, rgb)`` advances the flakes by ``dt``
    seconds and plots them onto ``frame`` (H×W×3 uint8, written in place). Flake count tracks
    ``wind × frame_width / 320`` (the faithful density, scaled to the wide width); the first half of the
    flakes draws the faithful second dot one row down. Resizing (widescreen toggle) re-seeds smoothly by
    keeping the common prefix of the flake array."""

    def __init__(self):
        self._rng = np.random.default_rng(_SEED)
        self._x = np.empty(0, np.float64)      # flake positions (frame space)
        self._y = np.empty(0, np.float64)
        self._w = 0

    def draw(self, frame, wind: int, dt: float, rgb=(255, 255, 255)) -> None:
        h, w = frame.shape[:2]
        n = int(round(wind * w / 320.0)) if wind > 0 else 0
        if n <= 0:
            return
        if w != self._w:                                          # width change -> re-scatter (rare)
            self._x = self._rng.uniform(0, w, size=len(self._x))
            self._w = w
        if n > len(self._x):                                      # wind rose -> spawn the difference
            add = n - len(self._x)
            self._x = np.concatenate([self._x, self._rng.uniform(0, w, size=add)])
            self._y = np.concatenate([self._y, self._rng.uniform(0, _VIEW_H, size=add)])
        dt = min(max(dt, 0.0), 0.25)                              # clamp a stall so flakes never teleport far
        self._x += VX * dt
        self._y += VY * dt
        self._x %= w                                              # wrap horizontally across the wide frame
        self._y %= _VIEW_H                                        # the faithful 175-row wrap
        xi = self._x[:n].astype(np.int32)
        yi = self._y[:n].astype(np.int32)
        # the faithful sub-byte dither: a per-flake stable ±[0..6] px offset stands in for the rol-bit select
        xj = (xi + (np.arange(n) * 2) % 7) % w
        frame[yi, xj] = rgb                                       # primary dots
        half = n >> 1                                             # first half: the second dot one row down
        y2 = yi[:half] + 1
        ok = y2 < _VIEW_H
        frame[y2[ok], xj[:half][ok]] = rgb
