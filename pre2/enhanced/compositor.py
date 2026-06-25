"""Modern RGB/RGBA object compositor — the enhanced DISPLAY-time path.

Composites a display subframe entirely in RGB/RGBA from two source-cadence :class:`EnhancedFrameState`s:
start from the (cached) background, then for each sprite in draw order blit its RGBA texture at the
position interpolated between the previous and current source frames by ``alpha``. No planar buffers, no
deplanarize, no faithful rasterization at display time, no whole-frame blend — sprites move individually.

``alpha`` in [0,1]: 0 = previous source positions, 1 (or ``prev is None``) = current verbatim. Object
identity across frames is ``base_id`` (duplicates paired in draw order via a per-id queue), matching the
recovered :func:`pre2.recovered.render_interp.interpolate_frame` contract. Fixed-screen sprites
(``interpolate=False``: HUD / boss meter) are drawn at their anchor, never lerped. New sprites (no prev
match) appear at their current position; despawned ones simply aren't in ``cur``.
"""
from __future__ import annotations

from collections import defaultdict, deque


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
    prev_by_id: dict = defaultdict(deque)
    if interp:
        for inst in prev.sprites:
            prev_by_id[inst.base_id].append(inst)
    for inst in cur.sprites:
        x, y = inst.anchor_x, inst.anchor_y
        if interp and inst.interpolate:
            q = prev_by_id.get(inst.base_id)
            if q:
                p = q.popleft()
                x = round(p.anchor_x + (inst.anchor_x - p.anchor_x) * alpha)
                y = round(p.anchor_y + (inst.anchor_y - p.anchor_y) * alpha)
        _blit(frame, inst.rgba, x, y)
    return frame
