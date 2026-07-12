"""Recovered: the scripted-camera-scroll / LEVELG falling-snow routine — 1030:3922.

Only ``LEVELG.SQZ`` (level index 0x0F, a hidden auto-scroll bonus stage) carries a script; every other level
points ``script_ptr`` at the shared empty ``-1`` table. The routine has two halves:

* STATE (3922..3968) — ``scroll_script_state``: advance the frame counter; every 4th frame walk the current
  6-byte script entry ``{threshold, delta, clamp, next_threshold}`` and ramp the accumulated ``wind`` by
  ``delta`` (clamped to ``[0, clamp]``, signed), advancing to the next entry at its threshold. ``wind`` doubles
  as the snow flake count and drives the player push.
* RENDER (3963..39DE) — ``scroll_script_snow``: when ``wind`` is non-zero, OR-plot ``wind`` white pixels from
  the flake array, advancing each flake and refreshing one slot from the ``rng_lcg`` generator. Returns the
  plotted pixels for the faithful renderer (``draw_snow``); mutates the flake array (cosmetic) AND the shared
  gameplay rng (``rng_a..rng_d`` — byte-exact matters).

**Offset-free**: these functions operate on a human-named *view* (see ``pre2.views.dgroup_view``); the DGROUP
layout lives entirely in that view, not here. Verified byte-exact vs the ASM (state sweep + a live LEVELG snow
frame: rng, flake array, and all 260 plots).
"""
from __future__ import annotations

from pre2.islands import oracle_link
from pre2.recovered.prng import rng_lcg


def _s16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


@oracle_link("1030:3922", "state half: frame counter + script-entry advance + wind accumulate/clamp",
             status="VERIFIED", merge_target="native_scroll_script (main-loop 0256)")
def scroll_script_state(s) -> None:
    """The 3922 state half — mutate the scripted-scroll ``s`` (a ``ScrollScriptView``) in place."""
    counter = (s.frame_counter + 1) & 0xFFFF             # [asm 3922] inc the frame counter
    s.frame_counter = counter
    if (s.tick & 3) != 0:                                # [asm 3926] only every 4th frame
        return
    entry = s.script                                     # [asm 3930] load the current entry (before any advance)
    if entry.threshold == 0xFFFF:                        # [asm 3934] end-of-script marker -> nothing more
        return
    scroll = s.wind
    if entry.threshold < counter:                        # [asm 3939 jae] accumulate once the threshold is passed
        scroll = (scroll + entry.delta) & 0xFFFF         # [asm 3940] += delta
        if not (entry.next_threshold >= counter):        # [asm 3944 jae] advance at the next threshold
            s.script_ptr = (s.script_ptr + 6) & 0xFFFF   # [asm 3949] step to the next 6-byte entry
    s.wind = min(max(0, _s16(scroll)), _s16(entry.clamp)) & 0xFFFF   # [asm 3951-3960] clamp to [0, clamp] (signed)


@oracle_link("1030:396A", "render half: falling-snow plot -- steps the flake array + rng and returns the white "
             "VGA pixels to OR-plot (ES=A000, GC OR-mode, map-mask 0x0F)", status="VERIFIED",
             merge_target="native snow render effect")
def scroll_script_snow(s) -> list[tuple[int, int]]:
    """The 3922 render half (3963..39DE): the LEVELG falling snow, over a ``ScrollScriptView`` ``s``.

    Runs ONLY when ``wind`` is non-zero (the wind magnitude *is* the flake count). Walks the first ``wind``
    flakes, advancing each by ``+0x4F`` (wrapping the camera-relative position into the top ``0x1B58`` bytes of
    the page), OR-plots one white pixel per flake (plus a second one row down for the first half), then refreshes
    one array slot from three ``rng_lcg`` draws. Mutates the flake array (cosmetic, excluded from the digest)
    **and** the shared gameplay rng (byte-exact matters).

    Returns ``(page_relative_byte_offset, bit_mask)`` pairs (screen space, HUD rows excluded); empty when the
    wind is zero. ``draw_snow`` OR's them white into all four planes."""
    plots: list[tuple[int, int]] = []
    wind = s.wind                                        # [asm 3963/3980] wind == flake count; 0 -> no snow
    if wind == 0:
        return plots
    dx = wind
    bp = dx >> 1                                         # [asm 3984-3986] first half also gets a second pixel
    cam = s.camera_x
    flakes = s.flakes
    i = 0
    while True:
        v = (flakes[i] + 0x4F) & 0xFFFF                  # [asm 398B] advance this flake, persist it
        flakes[i] = v
        al = v & 0xFF
        cl = (((al << 1) | (al >> 7)) & 3)              # [asm 3993-3995] rol al,1 & 3 -> sub-byte bit select
        bx = (v - cam) & 0xFFFF                          # [asm 3998] camera-relative
        bx &= 0x1FFF                                     # [asm 399C] and bh,0x1f -> wrap into the 0x2000 page
        if bx >= 0x1B58:                                 # [asm 399F-39A9] past row 175 -> wrap up, write back
            bx = (bx - 0x1B58) & 0xFFFF
            flakes[i] = bx
        i += 1                                           # [asm 398E] lodsw advanced si (after the write-back)
        ax = (0x302 << cl) & 0xFFFF                      # [asm 39B0-39B3] al = 0x02<<cl bit, ah = 0x03<<cl
        plots.append((bx, ax & 0xFF))                    # [asm 39B5] primary pixel
        if dx > bp:                                      # [asm 39BC] jbe -> only the first half
            plots.append(((bx + 0x28) & 0xFFFF, (ax >> 8) & 0xFF))   # [asm 39C0] second pixel one row down
        dx = (dx - 1) & 0xFFFF                           # [asm 39C4]
        if dx == 0:                                      # [asm 39C5]
            break
    a, b, c, d = s.rng_a, s.rng_b, s.rng_c, s.rng_d      # refresh one flake slot from the rng
    a, b, c, d, r1 = rng_lcg(a, b, c, d)                # [asm 39C7] index (word) = 2*r1
    a, b, c, d, r2 = rng_lcg(a, b, c, d)                # [asm 39D0-39D3] dh = r2 (high byte)
    a, b, c, d, r3 = rng_lcg(a, b, c, d)                # [asm 39D5-39D8] al = r3 (low byte)
    flakes[r1] = ((r2 << 8) | r3) & 0xFFFF              # [asm 39DA]
    s.rng_a, s.rng_b, s.rng_c, s.rng_d = a, b, c, d
    return plots


@oracle_link("1030:39B5", "the snow plot itself: OR each (offset, bit-mask) white pixel into all four planes on "
             "the render page (ES=A000, map-mask 0x0F, OR mode)", status="VERIFIED",
             merge_target="apply_gameplay_effects")
def draw_snow(planes, plots, page: int) -> None:
    """OR the ``scroll_script_snow`` pixels into ``planes`` (4 EGA planes) on ``page``.

    Each plot is ``(page_relative_byte_offset, bit_mask)``; the mask is OR'd into every plane, so the pixel is
    colour 15 (white) — matching ``xchg es:[bx], al`` under map-mask 0x0F + GC OR mode. Mirrors ``draw_fireflies``
    (dos_re has no Set/Reset, so all four planes take the CPU bit)."""
    for off, mask in plots:
        a = (page + off) & 0xFFFF
        planes[0][a] |= mask
        planes[1][a] |= mask
        planes[2][a] |= mask
        planes[3][a] |= mask
