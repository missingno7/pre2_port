"""The gameplay effect overlays applied ON TOP of the core ``render_frame`` output.

``render_frame`` reproduces the background + sprites + HUD, but three effect passes draw over that result
each frame and are captured from VM state at their OWN hook instants (the core frame can't reconstruct
them at the 6772 commit because they have transient state):

  * point particles (``4B8E``)        — one-shot; drawn+killed each frame, so snapshot at 4B8E entry
  * foreground tiles (``3721``)       — redraw flag-0x40 tiles OVER sprites; the active list is rebuilt
                                        each frame, so snapshot at the 3732 pass entry
  * firefly swarm (``54AB``)          — persistent slots; readable at the 6772 commit

This module bundles those three captured states and applies their recovered draws in the engine's
on-top order (particles, then foreground tiles, then fireflies) so both faithful render paths share ONE
effect-compositing step instead of duplicating it. A ``None`` field means that effect is inactive this
frame.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from pre2.bridge.fireflies import FireflyState, read_fireflies
from pre2.bridge.foreground_tiles import ForegroundState
from pre2.bridge.particles import ParticleFrame
from pre2.recovered.fireflies import draw_fireflies
from pre2.recovered.foreground_tiles import render_foreground_tiles
from pre2.recovered.particles import draw_particles
from pre2.recovered.scroll_script import draw_snow


@dataclass(frozen=True)
class GameplayEffects:
    """The four effect-overlay states captured for one displayed frame (any may be ``None``)."""
    particles: Optional[ParticleFrame] = None       # snapshotted at 4B8E entry (pre-kill)
    foreground: Optional[ForegroundState] = None    # snapshotted at the 3732 pass entry
    fireflies: Optional[FireflyState] = None         # read at the 6772 commit (slots persist)
    snow: Optional[list] = None                      # LEVELG falling snow (scroll_script_snow plot list)


def capture_gameplay_effects(mem, *, particle_frame=None, foreground_frame=None) -> GameplayEffects:
    """Bundle the stashed particle/foreground captures with the fireflies (read live at the commit)."""
    ff = read_fireflies(mem)
    # The LEVELG snow pixels are computed by native_scroll_script (the 3922 render half) and stashed on the
    # state; empty/absent on every other level (wind [0x6bf6] == 0).
    snow = getattr(mem, "snow_plots", None)
    return GameplayEffects(
        particles=particle_frame,
        foreground=foreground_frame,
        fireflies=ff if ff.slots else None,
        snow=snow if snow else None,
    )


def apply_gameplay_effects(planes, page: int, fx: Optional[GameplayEffects]) -> None:
    """Draw the active effect overlays onto ``planes`` (the rendered core frame) in engine on-top order."""
    if fx is None:
        return
    if fx.particles is not None:
        p = fx.particles
        draw_particles(planes, p.particles, p.cam_col, p.cam_row, p.y_bias, page, p.cos, p.sin)
    if fx.foreground is not None:
        render_foreground_tiles(planes, fx.foreground)
    if fx.fireflies is not None:
        f = fx.fireflies
        draw_fireflies(planes, f.slots, f.cam_col, f.cam_row, page)
    if fx.snow is not None:
        draw_snow(planes, fx.snow, page)
