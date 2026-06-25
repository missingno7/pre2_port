"""Modern RGB/RGBA object compositor — the enhanced DISPLAY-time path.

Composites a display subframe entirely in RGB/RGBA from two source-cadence :class:`EnhancedFrameState`s:
start from the (cached) background, then for each sprite in draw order blit its RGBA texture at the
position interpolated between the previous and current source frames by ``alpha``. No planar buffers, no
deplanarize, no faithful rasterization at display time, no whole-frame blend — sprites move individually.

``alpha`` in [0,1]: 0 = previous source placement, 1 (or ``prev is None``) = current verbatim. Object
identity across frames is the persistent ``handle`` — stable across BOTH the walk/blink animation (which
changes sprite_id/base_id every frame) AND active-list compaction on spawn (which shifts slot indices). A
handle can be REUSED after a despawn, so interpolation is gated on a small per-frame WORLD move
(``_MAX_INTERP_MOVE``); a large jump (reuse or a teleport) snaps to the current position instead. The WORLD
position is interpolated (not screen — screen folds in the per-animation-frame draw offset, which would
inject ±1 shake) and the CURRENT frame's texture is drawn at the interpolated placement + ``cur.tex_off``.
Fixed-screen sprites (``interpolate=False``: HUD / boss meter) are drawn at their current placement, never
lerped. New objects (no prev handle) appear at their current placement; despawned ones simply aren't in ``cur``.
"""
from __future__ import annotations

import numpy as np

# Max per-source-frame WORLD move (px) we will interpolate. Real object motion is a few px/frame; a larger
# jump means the handle was reused for a different object (despawn+spawn) or a genuine teleport -> snap.
_MAX_INTERP_MOVE = 32
# Don't interpolate the background across a huge camera jump (level load / teleport) -> snap to current.
_MAX_CAM_SCROLL = 64
VIEWPORT_H = 176   # gameplay viewport rows (SCROLL_HEIGHT 0xB0); the HUD strip below it never scrolls


def _scroll_bg(bg, dx: int, dy: int):
    """Return the background scrolled right/down by (dx, dy) px for camera interpolation, replicating the
    exposed edge (the newly-revealed strip is a thin approximation -- the true content is one source frame
    away). dx/dy are small (sub-source-frame camera deltas)."""
    if dx == 0 and dy == 0:
        return bg
    out = np.roll(bg, (dy, dx), axis=(0, 1))
    if dx > 0:
        out[:, :dx] = out[:, dx:dx + 1]
    elif dx < 0:
        out[:, dx:] = out[:, dx - 1:dx]
    if dy > 0:
        out[:dy, :] = out[dy:dy + 1, :]
    elif dy < 0:
        out[dy:, :] = out[dy - 1:dy, :]
    return out


def _blit(frame, rgba, x: int, y: int) -> None:
    """Alpha-keyed blit of an H×W×4 texture onto an RGB frame at (x, y), clipped to the frame."""
    fh, fw = frame.shape[:2]
    h, w = rgba.shape[:2]
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(fw, x + w), min(fh, y + h)
    if x1 <= x0 or y1 <= y0:
        return
    sub = rgba[y0 - y:y1 - y, x0 - x:x1 - x]
    mask = sub[..., 3] > 0
    frame[y0:y1, x0:x1][mask] = sub[..., :3][mask]


def compose(cur, prev, alpha: float):
    """Render one display subframe (RGB) from ``cur`` (and ``prev`` for interpolation) at ``alpha``."""
    interp = prev is not None and alpha < 1.0
    inv = 1.0 - alpha
    # Camera scroll: show the background at the interpolated camera (cur shifted back toward prev). Objects
    # are then glued to it by the same camera shift; their own world motion is interpolated on top.
    bg_dx = bg_dy = 0
    if interp:
        cdx, cdy = cur.camera[0] - prev.camera[0], cur.camera[1] - prev.camera[1]
        if abs(cdx) <= _MAX_CAM_SCROLL and abs(cdy) <= _MAX_CAM_SCROLL:
            bg_dx, bg_dy = round(inv * cdx), round(inv * cdy)
    frame = cur.background_rgb.copy()
    if bg_dx or bg_dy:
        # Scroll only the gameplay VIEWPORT rows; the HUD strip (rows VIEWPORT_H..) is fixed-screen.
        frame[:VIEWPORT_H] = _scroll_bg(cur.background_rgb[:VIEWPORT_H], bg_dx, bg_dy)
    prev_by_handle = {inst.handle: inst for inst in prev.sprites} if interp else {}
    for inst in cur.sprites:
        if inst.interpolate:                          # world sprite -> glued to the scrolled background
            sx, sy = inst.screen_x + bg_dx, inst.screen_y + bg_dy
            p = prev_by_handle.get(inst.handle) if interp else None
            if p is not None:
                wdx, wdy = inst.world_x - p.world_x, inst.world_y - p.world_y
                if abs(wdx) <= _MAX_INTERP_MOVE and abs(wdy) <= _MAX_INTERP_MOVE:
                    # Move by the WORLD delta (smooth) applied to the current placement, keeping the
                    # per-animation-frame draw offset fixed at the current frame -- avoids the ±1 screen
                    # jitter animation offsets inject. Large jump (handle reuse/teleport) -> snap (no interp).
                    sx -= round(inv * wdx)
                    sy -= round(inv * wdy)
        else:                                         # fixed-screen HUD / boss meter -> no scroll, no interp
            sx, sy = inst.screen_x, inst.screen_y
        _blit(frame, inst.rgba, sx + inst.tex_off_x, sy + inst.tex_off_y)
    return frame
