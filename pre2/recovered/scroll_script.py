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
from pre2.recovered.prng import rng_lcg

_COUNTER = 0x2DBE     # [asm 3922] the scripted-scroll frame counter
_PTR = 0x2DBC         # [asm 3930] pointer to the current 6-byte script entry
_SCROLL = 0x6BF6      # [asm 3940] the accumulated WIND amount (flake count + player push + snow render half)
_TICK = 0x6BD5        # [asm 3926] the free-running frame counter (the &3 4-frame gate)

_FLAKES = 0x6CA9      # [asm 3988] the 0x100-word flake-position array (level_init seeds it from the rng)
_CAM = 0x2DE4         # [asm 3998] the horizontal camera offset the flake position is taken relative to
_PAGE = 0x2DD8        # [asm 39AC] the draw-page byte base added before the VGA plot


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


@oracle_link("1030:396A", "render half: falling-snow plot -- steps the flake array [0x6CA9] + rng [0x2CEC] and "
             "returns the white VGA pixels to OR-plot (ES=A000, GC OR-mode, map-mask 0x0F)",
             status="VERIFIED", merge_target="native snow render effect")
def scroll_script_snow(rb, rw, wb, ww) -> list[tuple[int, int]]:
    """Reproduce the 3922 render half (3963..39DE): the LEVELG falling snow.

    Runs ONLY when the wind ``[0x6BF6]`` is non-zero; the wind magnitude *is* the flake count. Each frame it
    walks the first ``wind`` words of the flake array ``[0x6CA9]``, advancing every flake by ``+0x4F`` (wrapping
    the camera-relative position back into the top ``0x1B58`` bytes of the page), OR-plots one white pixel per
    flake into the draw page (plus a second pixel one row down for the first half of the flakes), then refreshes
    one array slot from three ``rng_lcg`` draws. Mutates ``[0x6CA9..]`` (cosmetic, excluded) **and** ``[0x2CEC]``
    (the shared deterministic gameplay rng — byte-exact matters), via ``wb``/``ww``.

    Returns the plotted pixels as ``(page_relative_byte_offset, bit_mask)`` where the byte offset is
    ``camera_relative_position`` (0..0x1B57, i.e. screen space, HUD rows excluded) and the bit mask is a single
    (primary) or double (secondary) set bit to OR white into all four planes. Empty list when the wind is zero."""
    plots: list[tuple[int, int]] = []
    wind = rw(_SCROLL)                                    # [asm 3963/3980] wind == flake count; 0 -> no snow
    if wind == 0:
        return plots
    dx = wind
    bp = dx >> 1                                          # [asm 3984-3986] first half also gets a second pixel
    cam = rw(_CAM)
    si = _FLAKES
    while True:
        v = (rw(si) + 0x4F) & 0xFFFF                      # [asm 398B] advance this flake, persist it
        ww(si, v)
        al = v & 0xFF
        cl = (((al << 1) | (al >> 7)) & 3)               # [asm 3993-3995] rol al,1 & 3 -> sub-byte bit select
        bx = (v - cam) & 0xFFFF                           # [asm 3998] camera-relative
        bx &= 0x1FFF                                      # [asm 399C] and bh,0x1f -> wrap into the 0x2000 page
        if bx >= 0x1B58:                                  # [asm 399F-39A9] past row 175 -> wrap up, write back
            bx = (bx - 0x1B58) & 0xFFFF
            ww(si, bx)
        si = (si + 2) & 0xFFFF                            # [asm 398E] lodsw advanced si (after the write-back)
        ax = (0x302 << cl) & 0xFFFF                       # [asm 39B0-39B3] al = 0x02<<cl bit, ah = 0x03<<cl
        plots.append((bx, ax & 0xFF))                     # [asm 39B5] primary pixel
        if dx > bp:                                       # [asm 39BC] jbe -> only the first half
            plots.append(((bx + 0x28) & 0xFFFF, (ax >> 8) & 0xFF))   # [asm 39C0] second pixel one row down
        dx = (dx - 1) & 0xFFFF                            # [asm 39C4]
        if dx == 0:                                       # [asm 39C5]
            break
    a, b, c, dd = rb(0x2CEC), rb(0x2CED), rb(0x2CEE), rw(0x2CEF)     # refresh one flake slot from the rng
    a, b, c, dd, r1 = rng_lcg(a, b, c, dd)               # [asm 39C7] index (word) = 2*r1
    a, b, c, dd, r2 = rng_lcg(a, b, c, dd)               # [asm 39D0-39D3] dh = r2 (high byte)
    a, b, c, dd, r3 = rng_lcg(a, b, c, dd)               # [asm 39D5-39D8] al = r3 (low byte)
    ww((_FLAKES + ((r1 << 1) & 0xFFFF)) & 0xFFFF, ((r2 << 8) | r3) & 0xFFFF)   # [asm 39DA]
    wb(0x2CEC, a); wb(0x2CED, b); wb(0x2CEE, c); ww(0x2CEF, dd)
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
