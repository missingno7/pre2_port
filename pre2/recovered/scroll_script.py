"""Recovered: the scripted-camera-scroll STATE update — 1030:3922 (state half, 3922..3968).

Only ``LEVELG.SQZ`` (level index 0x0F, a hidden auto-scroll bonus stage) carries a script; every other level
points ``[0x2DBC]`` at the shared empty ``-1`` table. Per frame the routine advances the frame counter
``[0x2DBE]``; every 4th frame (``[0x6BD5] & 3 == 0``) it walks the current 6-byte script entry at ``[0x2DBC]``
(``{threshold, delta, clamp, next_threshold@+6}``): once the counter has passed the entry's threshold it ramps
the accumulated vertical scroll ``[0x6BF6]`` by ``delta`` (clamped to ``[0, clamp]``, signed) and advances to the
next entry when the counter reaches the next threshold. ``[0x6BF6]`` is the amount the RENDER half (3922:396A..,
a VGA raster smooth-scroll) then pans the display by — that pixel half is the faithful renderer's job.

Verified byte-exact vs the ASM 3922 state half over the LEVELG gap memory + a frame sweep (all script stages +
the &3 gate).
"""
from __future__ import annotations

from pre2.islands import oracle_link

_COUNTER = 0x2DBE     # [asm 3922] the scripted-scroll frame counter
_PTR = 0x2DBC         # [asm 3930] pointer to the current 6-byte script entry
_SCROLL = 0x6BF6      # [asm 3940] the accumulated vertical scroll amount (drives the render half)
_TICK = 0x6BD5        # [asm 3926] the free-running frame counter (the &3 4-frame gate)


def _s16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


@oracle_link("1030:3922", "state half: [0x2DBE] counter + [0x2DBC] entry advance + [0x6BF6] scroll accumulate/clamp",
             status="VERIFIED", merge_target="native_scroll_script (main-loop 0256)")
def scroll_script_state(rb, rw) -> dict:
    """Return the DGROUP word writes of the 3922 state half. ``rb``/``rw`` read DGROUP bytes/words."""
    writes: dict[int, int] = {}
    counter = (rw(_COUNTER) + 1) & 0xFFFF                 # [asm 3922] inc [0x2DBE]
    writes[_COUNTER] = counter
    if (rb(_TICK) & 3) != 0:                              # [asm 3926] only every 4th frame
        return writes
    bx = rw(_PTR)
    thr = rw(bx)                                          # [asm 3934] entry threshold
    if thr == 0xFFFF:                                     # [asm 3934] end-of-script marker -> nothing more
        return writes
    scroll = rw(_SCROLL)
    if thr < counter:                                    # [asm 3939 jae] accumulate once the threshold is passed
        scroll = (scroll + rw(bx + 2)) & 0xFFFF          # [asm 3940] += delta
        if not (rw(bx + 6) >= counter):                  # [asm 3944 jae] advance at the next threshold
            writes[_PTR] = (bx + 6) & 0xFFFF             # [asm 3949] [0x2DBC] += 6
    clamped = min(max(0, _s16(scroll)), _s16(rw(bx + 4)))   # [asm 3951-3960] clamp to [0, entry.clamp] (signed)
    writes[_SCROLL] = clamped & 0xFFFF
    return writes
