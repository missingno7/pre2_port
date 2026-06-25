"""Modern RGB/RGBA object compositor — the enhanced DISPLAY-time path.

Composites a display subframe entirely in RGB/RGBA from two source-cadence :class:`EnhancedFrameState`s:
start from the (cached) background, then for each sprite in draw order blit its RGBA texture at the
position interpolated between the previous and current source frames by ``alpha``. No planar buffers, no
deplanarize, no faithful rasterization at display time, no whole-frame blend — sprites move individually.

``alpha`` in [0,1]: 0 = previous source placement, 1 (or ``prev is None``) = current verbatim. Object
identity across frames is the ACTIVE-LIST ``slot`` — stable across the walk/blink animation; matching on
``base_id`` would split one animating object into a new identity every frame (the cause of the stutter we
fixed). The LOGICAL placement (``screen_x``/``screen_y``) is interpolated and the CURRENT animation frame's
texture is drawn (positions interpolate; the sprite image swaps discretely per source frame) at
``interp_placement + cur.tex_off``. Fixed-screen sprites (``interpolate=False``: HUD / boss meter) are drawn
at their current placement, never lerped. New objects (no prev slot) appear at their current placement;
despawned ones simply aren't in ``cur``.
"""
from __future__ import annotations


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
    frame = cur.background_rgb.copy()
    interp = prev is not None and alpha < 1.0
    prev_by_slot = {inst.slot: inst for inst in prev.sprites} if interp else {}
    for inst in cur.sprites:
        sx, sy = inst.screen_x, inst.screen_y
        if interp and inst.interpolate:
            p = prev_by_slot.get(inst.slot)
            if p is not None:
                # Move by the WORLD delta (smooth) applied to the CURRENT screen placement, so the
                # per-animation-frame draw offset (and camera) stay fixed at the current frame -- avoids the
                # ±1 screen jitter that animation offsets inject and that interpolation would amplify.
                sx -= round((1.0 - alpha) * (inst.world_x - p.world_x))
                sy -= round((1.0 - alpha) * (inst.world_y - p.world_y))
        _blit(frame, inst.rgba, sx + inst.tex_off_x, sy + inst.tex_off_y)
    return frame
