"""The windowed vertical-scroll copy (1030:9C87) — the game-over scene's background present step.

Each frame the game-over screen copies a 176-row window of the diorama out of an off-screen staging
buffer into the back page, offset by a vertical scroll counter that ramps the diorama up. The ASM
(``9C87``, EGA setup ``453B`` = write mode 1 / map mask 0x0F) is a 4-plane latched ``rep movsb`` of
0x1B80 bytes (= 176 * 0x28, the viewport) from ``A000:(0x3F40 + 0x28*[0x6BC4])`` to ``A000:[0x2DD8]``.

In plane space this is just: for each plane, copy ``count`` bytes from ``src_base + stride*scroll`` to
``dest_page``. The source planes are either the VRAM staging (for the lockstep verify, ``src_base``
=0x3F40) or the decoded diorama image directly (for the faithful background compose, ``src_base``=0 —
skipping the VRAM staging entirely).
"""
from __future__ import annotations

from typing import Sequence

_STRIDE = 0x28
_COUNT = 0x1B80          # 176 rows * 0x28 = the viewport window
_STAGING = 0x3F40        # the off-screen staging base in VRAM (9C87's source)


def window_scroll_copy(dst_planes: Sequence[bytearray], src_planes: Sequence[bytes], scroll: int,
                       dest_page: int, *, src_base: int = _STAGING, count: int = _COUNT,
                       stride: int = _STRIDE) -> None:
    """Copy a scrolled ``count``-byte window from ``src_planes`` to ``dst_planes`` on every plane.

    Mirrors 9C87's write-mode-1 latched ``rep movsb`` (all 4 planes copied together). ``scroll`` is the
    vertical offset ([0x6BC4]); the window starts at ``src_base + stride*scroll`` in the source planes.
    """
    src_off = (src_base + stride * scroll) & 0xFFFF
    for p in range(4):
        sp = src_planes[p]
        dp = dst_planes[p]
        s = src_off
        di = dest_page & 0xFFFF
        for _ in range(count):
            dp[di] = sp[s]
            s = (s + 1) & 0xFFFF
            di = (di + 1) & 0xFFFF
