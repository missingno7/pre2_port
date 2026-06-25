"""Modern RGB/RGBA object compositor — the enhanced DISPLAY-time path.

Composites a display subframe entirely in RGB/RGBA from two source-cadence :class:`EnhancedFrameState`s:
start from the background, then for each sprite in draw order blit its RGBA texture at the position
interpolated between the previous and current source frames by ``alpha``. No planar buffers, no deplanarize,
no faithful rasterization at display time, no whole-frame blend — sprites move individually.

CAMERA SCROLL is layered: the parallax BACKDROP (sky/mountains -- ``backdrop_rgb``, the fixed base layer) is
held perfectly still, and only the scrolling TILE layer (``background_rgb != backdrop_rgb``) is moved to the
interpolated camera. Moving the whole composited background uniformly would shake the fixed backdrop. The
tile layer is moved two-source: the bulk from the current frame shifted back toward prev, the trailing edge
exposed by the scroll filled from the PREVIOUS frame (real revealed content -- not an edge-replicated smear).
When ``backdrop_rgb`` is absent the compositor falls back to a uniform whole-viewport shift.

The effect OVERLAY (foreground tiles + particles + fireflies) is composited LAST, OVER the sprites and
camera-scrolled like the tile layer (foreground tiles belong in front of sprites; particles/fireflies on
top). v1: the overlay scrolls with the camera but its own per-particle motion is not yet velocity-interpolated.

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
    """Fallback (no backdrop layer): scroll the whole background right/down by (dx, dy), replicating the
    exposed edge. Used only when ``backdrop_rgb`` is absent; the layered path below is preferred."""
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


def _scroll_tile_layer(cur_bg, cur_mask, prev_bg, prev_mask, cdx, cdy, alpha):
    """Scroll ONLY the foreground tile layer to the interpolated camera, two-source: the bulk comes from the
    current frame shifted back toward prev by (1-alpha)*delta; pixels exposed at the trailing edge are filled
    from the PREVIOUS frame (real content, not an edge-replicated smear). Returns (rgb, mask) of the tile
    layer at the interp camera; the caller composites it over the FIXED backdrop. Inputs are viewport-height
    slices; ``*_mask`` marks tile (non-backdrop) pixels."""
    h, w = cur_bg.shape[:2]
    inv = 1.0 - alpha
    cy, cx = int(round(inv * cdy)), int(round(inv * cdx))     # cur sampled at index - (cy,cx)
    # prev's offset is the EXACT integer complement (ay = cdy - cy), so a world point maps to the same
    # output pixel from cur and prev -> the two layers meet seamlessly (no 1px gap showing the backdrop).
    ay, ax = cdy - cy, cdx - cx                                # prev sampled at index + (ay,ax)
    rr, cc = np.arange(h)[:, None], np.arange(w)[None, :]
    inb_cur = (rr - cy >= 0) & (rr - cy < h) & (cc - cx >= 0) & (cc - cx < w)
    inb_prev = (rr + ay >= 0) & (rr + ay < h) & (cc + ax >= 0) & (cc + ax < w)
    cur_rgb = np.roll(cur_bg, (cy, cx), axis=(0, 1))
    cur_on = np.roll(cur_mask, (cy, cx), axis=(0, 1)) & inb_cur
    prev_rgb = np.roll(prev_bg, (-ay, -ax), axis=(0, 1))
    prev_on = np.roll(prev_mask, (-ay, -ax), axis=(0, 1)) & inb_prev
    rgb = np.where(cur_on[..., None], cur_rgb, prev_rgb)       # prefer current; fall back to previous
    return rgb, (cur_on | prev_on)


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
    # Camera scroll: show the world at the interpolated camera. The PARALLAX BACKDROP (sky/mountains) is
    # fixed-screen, so only the scrolling TILE layer is moved; objects are then glued to it by the same
    # camera shift; their own world motion is interpolated on top. (bg_dx/bg_dy glue the world sprites.)
    bg_dx = bg_dy = 0
    cdx = cdy = 0
    if interp:
        cdx, cdy = cur.camera[0] - prev.camera[0], cur.camera[1] - prev.camera[1]
        if abs(cdx) <= _MAX_CAM_SCROLL and abs(cdy) <= _MAX_CAM_SCROLL:
            bg_dx, bg_dy = round(inv * cdx), round(inv * cdy)
    frame = cur.background_rgb.copy()
    if bg_dx or bg_dy:
        h = VIEWPORT_H
        if cur.tile_mask is not None and prev.tile_mask is not None and cur.backdrop_rgb is not None:
            # Layered: hold the fixed backdrop still, scroll only the tile layer over it. The tile coverage is
            # the TRUE (colour-independent) mask from extraction -- a `bg != backdrop` test would miss tile
            # pixels that share the backdrop colour and leave them static ("see-through" holes during shake).
            tile_rgb, tile_mask = _scroll_tile_layer(cur.background_rgb[:h], cur.tile_mask[:h],
                                                     prev.background_rgb[:h], prev.tile_mask[:h], cdx, cdy, alpha)
            vp = cur.backdrop_rgb[:h].copy()
            vp[tile_mask] = tile_rgb[tile_mask]
            frame[:h] = vp
        else:
            # Fallback (no backdrop layer captured): uniform whole-viewport shift with edge replication.
            frame[:h] = _scroll_bg(cur.background_rgb[:h], bg_dx, bg_dy)
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

    # One-shot point particles (spider threads/sparkles) OVER the sprites, UNDER the foreground/firefly overlay
    # (engine order). Each is rewound along its own per-frame velocity for smooth sub-source-frame motion, and
    # glued to the scrolled world by the camera shift. They have no cross-frame identity (spawned+killed each
    # frame), so the interpolation uses the current particle's own velocity (exact at alpha=1).
    if cur.particles:
        pr = cur.particle_rgb
        fh, fw = frame.shape[:2]
        for (sx, sy, vx, vy) in cur.particles:
            if interp:
                px, py = sx + bg_dx - round(inv * vx), sy + bg_dy - round(inv * vy)
            else:
                px, py = sx, sy
            if 0 <= px < fw and 0 <= py < VIEWPORT_H:
                frame[py, px] = pr

    # Effect overlay (foreground tiles + fireflies) drawn OVER the sprites + particles, scrolled with the
    # camera like the tile layer (the effects are world-space). Foreground tiles must be in FRONT of sprites.
    if cur.overlay_mask is not None:
        h = VIEWPORT_H
        if interp and (bg_dx or bg_dy):
            p_rgb = prev.overlay_rgb[:h] if prev.overlay_mask is not None else cur.overlay_rgb[:h]
            p_mask = prev.overlay_mask[:h] if prev.overlay_mask is not None else cur.overlay_mask[:h]
            ov_rgb, ov_mask = _scroll_tile_layer(cur.overlay_rgb[:h], cur.overlay_mask[:h],
                                                 p_rgb, p_mask, cdx, cdy, alpha)
        else:
            ov_rgb, ov_mask = cur.overlay_rgb[:h], cur.overlay_mask[:h]
        frame[:h][ov_mask] = ov_rgb[ov_mask]

    # Fireflies (persistent swarm) OVER the foreground overlay (engine order: ...->fireflies). Matched by the
    # persistent slot index across frames and lerped in WORLD position (like sprites by handle), glued to the
    # scrolled world by the camera shift; a large jump (slot reuse) snaps.
    if cur.fireflies:
        fr = cur.firefly_rgb
        fh, fw = frame.shape[:2]
        prev_ff = {f[0]: f for f in prev.fireflies} if interp else {}
        for (idx, wx, wy, sx, sy) in cur.fireflies:
            px, py = sx + bg_dx, sy + bg_dy
            p = prev_ff.get(idx) if interp else None
            if p is not None:
                dwx, dwy = wx - p[1], wy - p[2]
                if abs(dwx) <= _MAX_INTERP_MOVE and abs(dwy) <= _MAX_INTERP_MOVE:
                    px -= round(inv * dwx)
                    py -= round(inv * dwy)
            if 0 <= px < fw and 0 <= py < VIEWPORT_H:
                frame[py, px] = fr
    return frame
