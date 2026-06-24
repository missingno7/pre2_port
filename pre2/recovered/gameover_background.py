"""The recovered GAME-OVER background — the diorama image composed straight from the decoded asset.

The full ASM chain (located + verified):
  * ``GAMEOVER.SQZ`` --unpack_sqz--> 32000 bytes = the diorama as 4 plane-major planes of 0x1F40
    (proven byte-identical to the decoded-asset segment ``[0x2875]``);
  * the staging fill (9B66 + setup) lays it into a 400-row off-screen buffer: the TOP 200 rows are
    black (night sky), the BOTTOM 200 rows are the asset (verified byte-exact vs the VRAM staging);
  * 9C87 window-scroll-copies a 176-row window at vertical offset ``[0x6BC4]`` to the displayed page.

This module reproduces the whole thing in plane space from the raw decoded asset bytes — no VM
framebuffer, no oracle capture. ``render_gameover_background`` is the RecoveredBackground producer the
SceneCompositor consumes.
"""
from __future__ import annotations

from typing import List, Sequence

from pre2.islands import oracle_link
from pre2.recovered.scene_scroll import window_scroll_copy

_PLANE = 0x1F40          # bytes per asset plane (200 rows * 0x28)
_STAGING_ROWS = 400
_STAGING_LEN = _STAGING_ROWS * 0x28      # 0x3E80
_SKY_ROWS = 200          # the staging's top half is black; the diorama is the bottom half


def deinterleave_asset(asset_bytes: bytes) -> List[bytes]:
    """Split the plane-major decoded GAMEOVER asset (4 * 0x1F40) into four EGA planes."""
    if len(asset_bytes) < 4 * _PLANE:
        raise ValueError(f"gameover asset too small: {len(asset_bytes)} < {4 * _PLANE}")
    return [asset_bytes[p * _PLANE:(p + 1) * _PLANE] for p in range(4)]


def build_staging(asset_planes: Sequence[bytes]) -> List[bytearray]:
    """Lay the diorama into the 400-row staging: top 200 rows black, bottom 200 rows = the asset."""
    staging = [bytearray(_STAGING_LEN + 0x180) for _ in range(4)]      # margin for the window read
    for p in range(4):
        staging[p][_SKY_ROWS * 0x28:_SKY_ROWS * 0x28 + _PLANE] = asset_planes[p]
    return staging


@oracle_link("1030:9B66",
             "game-over diorama staging assembly (the 9B66 fill): de-interleave the decoded GAMEOVER.SQZ "
             "asset (4 plane-major planes of 0x1F40) and lay it into the 400-row staging with the top 200 "
             "rows black (night sky) and the bottom 200 rows = the asset (verified byte-exact vs the VRAM "
             "staging), then window-scroll-copy (9C87) at [0x6BC4]. The faithful background composed "
             "straight from the decoded asset, no VRAM.",
             "VERIFIED", merge_target="render_scene")
def render_gameover_background(asset_bytes: bytes, scroll: int, page: int) -> List[bytearray]:
    """Compose the game-over diorama background for a given vertical scroll ([0x6BC4]) and dest page.

    Returns four EGA plane buffers (0x10000 each) with the 176-row diorama window written at ``page``.
    """
    staging = build_staging(deinterleave_asset(asset_bytes))
    planes = [bytearray(0x10000) for _ in range(4)]
    window_scroll_copy(planes, staging, scroll, page, src_base=0)
    return planes
