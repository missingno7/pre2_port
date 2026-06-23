"""Prehistorik 2 animated-tile cycle — recovered native state machine (pure).

The background tile graphics animate: a remap pointer ``[0x6BC2]`` selects one of a small
cycle of consecutive 256-byte tile-remap tables, and the renderer's grid walk translates each
animated tile id through the *current* table (``xlatb`` at 1030:36E7). Once per redraw the
cycle advances (``1030:367D..36A6``): gated on animated tiles being present this frame
(``[0x6BBD]``), throttled by a per-frame counter (``[0x6BD4]``), the pointer steps to the
next table and wraps after ``FRAME_COUNT`` tables — a continuously-evolving renderer-owned
visual state machine (like the palette fade), independent of how VGA realises the pixels.

Pure: no ``cpu``/``mem``/``dos_re`` imports. The VM↔memory translation lives in
``pre2/bridge/render_state.py``; the semantic model element is
``pre2.recovered.render_model.AnimationState``.
"""
from __future__ import annotations

from dataclasses import dataclass

from pre2.islands import oracle_link

__all__ = ["AnimStep", "ANIM_END", "ANIM_TABLE_STRIDE", "ANIM_WRAP_TABLES", "ANIM_BASE",
           "FRAME_COUNT", "FAST_THRESHOLD", "frame_index", "advance_animation"]

ANIM_END = 0x6988          # [asm 369E: cmp ax,0x6988] one past the last remap table
ANIM_TABLE_STRIDE = 0x100  # [asm 369C: inc ah] one 256-byte remap table per frame
ANIM_WRAP_TABLES = 3       # [asm 36A3: sub ah,3] wrap span -> cycle length
ANIM_BASE = ANIM_END - ANIM_WRAP_TABLES * ANIM_TABLE_STRIDE   # 0x6688, first table in the cycle
FRAME_COUNT = ANIM_WRAP_TABLES                                # 3 frames in the cycle
FAST_THRESHOLD = 0x14      # [asm 3686: cmp [0x6BF6],0x14] scroll speed that doubles the rate


@dataclass(frozen=True)
class AnimStep:
    """Raw animated-tile cycle inputs read from DGROUP (``1030:367D..36A6``): the remap
    pointer ``[0x6BC2]``, the throttle counter ``[0x6BD4]``, the animated-tiles-present gate
    ``[0x6BBD]``, and the scroll speed ``[0x6BF6]`` (``>=0x14`` halves the throttle period).
    Converted to the semantic :class:`~pre2.recovered.render_model.AnimationState`."""
    frame_ptr: int     # [0x6BC2]
    throttle: int      # [0x6BD4]
    active: bool       # [0x6BBD] != 0
    speed: int         # [0x6BF6]


def frame_index(frame_ptr: int) -> int:
    """Which frame of the cycle the remap pointer currently selects (0..FRAME_COUNT-1)."""
    return (((frame_ptr - ANIM_BASE) & 0xFFFF) >> 8) % FRAME_COUNT


def throttle_period(speed: int) -> int:
    """Frames between advances: 4 normally, 2 when scrolling fast (mask+1)."""
    return 2 if speed >= FAST_THRESHOLD else 4


@oracle_link("1030:367D",
             "advance the animated-tile remap cycle: if [0x6BBD] (animated tiles present) is "
             "0 do nothing; else inc throttle [0x6BD4] and, when (throttle & mask)==0 "
             "(mask=1 if [0x6BF6]>=0x14 else 3), step the remap pointer [0x6BC2] +0x100, "
             "wrapping at 0x6988 back by 3 tables. Returns (frame_ptr, throttle, advanced).",
             "VERIFIED", merge_target="render_frame")
def advance_animation(frame_ptr: int, throttle: int, active: bool, speed: int):
    """Recover ``1030:367D..36A6`` — one redraw's animated-tile cycle step.

    Returns ``(new_frame_ptr, new_throttle, advanced)`` matching the ASM's writes to
    ``[0x6BC2]``/``[0x6BD4]`` (``advanced`` = the pointer changed this frame)."""
    frame_ptr &= 0xFFFF
    throttle &= 0xFF
    if not active:                                       # [asm 367D/3682: gate on [0x6BBD]]
        return frame_ptr, throttle, False
    mask = 1 if speed >= FAST_THRESHOLD else 3           # [asm 3684..368D: speed picks mask]
    throttle = (throttle + 1) & 0xFF                     # [asm 368F: inc byte [0x6BD4]]
    if (throttle & mask) != 0:                           # [asm 3693/3697: throttle skip]
        return frame_ptr, throttle, False
    ax = ((((frame_ptr >> 8) + 1) & 0xFF) << 8) | (frame_ptr & 0xFF)  # [asm 369C: inc ah]
    if ax == ANIM_END:                                   # [asm 369E/36A1]
        ax = ((((ax >> 8) - 3) & 0xFF) << 8) | (ax & 0xFF)           # [asm 36A3: sub ah,3]
    return ax & 0xFFFF, throttle, True
