"""ENHANCED front-end map screens — widescreen + display-rate-smooth presentation of the CARTE world-map
scroll-in and the MODE-SELECT / PASSWORD scrolling map, from the same recovered ingredients the faithful
raster uses (the scenes carry them in ``FrontEndScene.enh``).

* **CARTE** ("carte", master, scroll_x): the stamped map master is a REAL 640x200 image (4 planes, 80 B/row)
  that the faithful screen reveals through a 312px CRTC window — widescreen simply shows MORE of it. The
  enhanced frame is a ``W``-wide window whose right edge tracks the reveal position (columns beyond it black,
  exactly the faithful column-by-column reveal), with SUB-PIXEL pan (two-slice lerp) so the 1px-per-retrace
  scroll is butter at any display rate.

* **MENU/PASSWORD** ("menu", ...): the background pans LINEARLY through the 0x2000-byte ring (the CRTC
  display start advances 4px/frame + the row sine-bounce; each display row is a CONTIGUOUS strip of the
  ring, rows 320px apart) — so a wide background is simply a WIDER contiguous strip of the same ring per
  row, seamless by construction (the linear pan is why a pure mod-320/200 pattern translation does NOT
  match: the horizontal pan bleeds into a slow row shift). The bg lives ONLY in planes 0|1 (seeded there;
  text never touches them) so the margins can't catch the double-buffered text ghosts; the TEXT lives on
  planes 2|3 and ORs its bits over the bg bits (screen colour = bg_bits | text_bits<<2), so the enhanced
  frame reproduces the exact faithful compose: wide linear bg indices | the faithful text indices
  (de-planarized from the scene's own planes 2,3 at its CRTC window) centred on top. The presentation pan
  position interpolates between consecutive scenes for display-rate smoothness.

Pure presentation: everything derives from the scene payload + palette; no game state is touched.
"""
from __future__ import annotations

import numpy as np

_ID16 = [(i, i, i) for i in range(16)] + [(0, 0, 0)] * 240   # identity palette -> de-planarize to indices


def _unpack_planes(data: bytes, offs, row_bytes: int, rows: int) -> np.ndarray:
    """De-planarize ``len(offs)`` MSB-first bitplanes at ``offs`` into an (rows, row_bytes*8) index array."""
    out = np.zeros((rows, row_bytes * 8), np.uint8)
    for p, off in enumerate(offs):
        arr = np.frombuffer(data, np.uint8, count=row_bytes * rows, offset=off).reshape(rows, row_bytes)
        out |= (np.unpackbits(arr, axis=1) << p)
    return out


class CarteEnh:
    """Widescreen + smooth presenter state for the carte scroll-in (master indices cached per master)."""

    def __init__(self):
        self._key = None
        self._idx = None                       # (200, 640) uint8 master indices

    def frame(self, master: bytes, scroll_f: float, palette, width: int) -> np.ndarray:
        if self._key != id(master):            # one master per carte entry (stamped fresh each visit)
            # screen row y shows master row y+1 (the ring blit's row phase, verified 0-diff vs faithful)
            self._idx = np.roll(_unpack_planes(master, (0, 0x3E80, 0x7D00, 0xBB80), 80, 200), -1, axis=0)
            self._key = id(master)
        pal = np.asarray(palette, np.uint8)[:16]
        rgbm = pal[self._idx]                                      # (200, 640, 3)
        W = width
        # The faithful window's RIGHT edge is the reveal FRONTIER (master col scroll_x - 8, the last blitted
        # byte column; verified 0-diff): the map slides in from the right over black. The wide window keeps
        # that frontier as its right edge and extends LEFT (more already-revealed map). Once the scroll parks
        # (scroll_x = 639) the reveal is complete -> show the full master (the faithful 312px window never
        # displays the map's last 8 columns at all; the enhanced end state completes it).
        frontier = 640.0 if scroll_f >= 639.0 else max(0.0, scroll_f - 8.0)
        a = frontier - W                                           # window left edge in master cols (may be < 0)
        a0 = int(np.floor(a))
        f = a - a0
        cols = np.arange(W + 1) + a0
        valid = (cols >= 0) & (cols < 640)
        buf = np.zeros((200, W + 1, 3), np.uint8)
        buf[:, valid] = rgbm[:, cols[valid]]
        if f > 1e-3:                                               # SUB-PIXEL pan: lerp two 1px-apart slices
            return (buf[:, :W].astype(np.float32) * (1.0 - f)
                    + buf[:, 1:].astype(np.float32) * f).astype(np.uint8)
        return buf[:, :W].copy()


class MenuEnh:
    """Widescreen + smooth presenter state for the mode-select / password map (linear ring sampling)."""

    def __init__(self):
        self._key = None                       # per-SCENE cache (a scene presents several smooth subframes)
        self._lin = None                       # the 65536px linear bg ring
        self._text = None                      # the faithful 320x200 text indices

    @staticmethod
    def pan_px(scene) -> int:
        """The scene's CRTC pan position in LINEAR ring pixels (display start bytes*8 + pel), wrap 65536."""
        return ((scene.page & 0x1FFF) * 8 + scene.pel) & 0xFFFF

    def frame(self, scene, pan_f: float, palette, width: int, scene_to_rgb) -> np.ndarray:
        pal = np.asarray(palette, np.uint8)[:16]
        if self._key != id(scene):
            # BG: planes 0|1 as one LINEAR 65536px ring; display row y = the contiguous strip starting at
            # pan + 320*y. A wide row is the same strip extended each side -- seamless (the pattern tiles).
            b0 = np.unpackbits(np.frombuffer(scene.planes[0], np.uint8, count=0x2000))   # MSB-first order
            b1 = np.unpackbits(np.frombuffer(scene.planes[1], np.uint8, count=0x2000))
            self._lin = b0 | (b1 << 1)                             # 65536px linear ring, bg indices 0..3
            # TEXT: planes 2|3 de-planarized at the scene's own CRTC window with an identity palette ->
            # index bits 4|8; OR over the bg bits = the exact faithful colour compose, centred.
            from dataclasses import replace
            zeros = bytes(len(scene.planes[0]))
            tscene = replace(scene, planes=(zeros, zeros, scene.planes[2], scene.planes[3]),
                             palette=tuple(_ID16))
            self._text = scene_to_rgb(tscene)[:, :, 0]             # (200, 320) indices in {0,4,8,12}
            self._key = id(scene)
        m = (width - 320) // 2
        pan = int(round(pan_f))
        off = (pan + (np.arange(width) - m)[None, :] + 320 * np.arange(200)[:, None]) & 0xFFFF
        idx = self._lin[off]                                       # (200, width) bg indices 0..3
        idx[:, m:m + 320] |= self._text
        return pal[idx]
