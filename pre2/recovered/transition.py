"""Prehistorik 2 screen-transition primitives — recovered native logic (pure).

The end-level effect (`1030:31D0` loop) is a **circular iris/vignette**: a circle of visible
image around the player shrinks to a point over several frames (darkness closing in) while the
palette fades. (The ledger long called this the "scale/zoom transition" — it is mathematically a
circle, not an image rescale: :func:`build_scaled_columns` reads a quarter-circle cos/sin table
× a shrinking radius; :func:`draw_scale_frame` clears everything *outside* that circle.) This
module recovers its renderer primitives, one bounded routine at a time, so the transition's pixel
work becomes clean recovered source (the multi-frame loop that *drives* them is a thin controller).

Primitives recovered here:

* :func:`clear_span` (``1030:32DE``) — clear a horizontal pixel span across all four
  EGA planes, with partial-byte edge masks. Used to wipe the area outside the iris circle.
* :func:`fade_palette` (``1030:6772``) — one step of a linear VGA DAC palette fade from
  a source palette toward a target, stepping every component by a growing amount until
  all arrive. Used by screen transitions (room/level/death fades).

Pure: no ``cpu``/``mem``/``dos_re`` imports. Plane/palette buffers are passed in; the
VM↔memory translation lives in ``pre2/bridge/``.
"""
from __future__ import annotations

from pre2.islands import oracle_link

__all__ = ["SCREEN_W", "SCREEN_H", "ROW_STRIDE", "DAC_COMPONENTS", "SCALE_COLUMNS",
           "clear_span", "fade_palette", "build_scaled_columns", "draw_scale_frame"]

DAC_COMPONENTS = 0x30   # 16 colours × 3 (R,G,B), 6-bit each [asm 6794: cx=0x30]
SCALE_COLUMNS = 0x41    # 65 source columns scanned per frame [asm 3244: cmp 0x40, jbe]


def _s16(v: int) -> int:
    """Interpret a 16-bit value as signed (the transition's compares are ``jge`` = signed)."""
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v

SCREEN_W = 0x140        # 320 px  [asm 32E3: cmp bx,0x140]
SCREEN_H = 0xC8         # 200 rows [asm 32EF: cmp dx,0xC8]
ROW_STRIDE = 0x28       # 40 bytes per row


@oracle_link("1030:32DE",
             "clear a horizontal pixel span [x, x+width) at screen row `row` across all "
             "4 EGA planes (partial-byte edge masks at both ends); VRAM byte = "
             "row*0x28 + page + x>>3",
             "VERIFIED", merge_target="render_frame")
def clear_span(planes, x: int, width: int, row: int, page: int,
               stride: int = ROW_STRIDE) -> None:
    """Recover ``1030:32DE`` — clear pixels ``[x, x+width)`` at ``row`` (all 4 planes).

    ``planes`` is the four EGA plane buffers; the caller has selected SC map mask 0x0F
    so every write hits all planes. No-op if out of bounds (matching the ASM guards).
    """
    if x >= SCREEN_W or width > SCREEN_W or row >= SCREEN_H:   # [asm 32E3..32F3]
        return
    di = (row * stride + page + (x >> 3)) & 0xFFFF             # [asm 32F5..3305]
    x_sub = x & 7
    if x_sub != 0 or width >= 8:                               # [asm 3308 jne / 330D cmp 8,jb]
        # left partial: keep the bits before x in this byte, clear from x to byte end.
        keep = (~(0xFF >> x_sub)) & 0xFF                       # [asm 331E-3322: not(0xFF>>cl)]
        for p in range(4):
            planes[p][di] &= keep                              # GC AND, map mask 0x0F
        di = (di + 1) & 0xFFFF                                 # [asm 3328]
        total = (width + x_sub) & 0xFFFF                       # [asm 3334-3337: cx += x&7]
        full = total >> 3                                      # [asm 333C-3340]
        for _ in range(full - 1 if full else 0):               # [asm 3342 je / 3344 dec cx / rep stosb]
            for p in range(4):
                planes[p][di] = 0
            di = (di + 1) & 0xFFFF
        cl = total & 7                                         # [asm 3349 pop cx / 334A and cl,7]
    else:                                                      # aligned + width<8 -> right partial only
        cl = width & 7
    if cl != 0:                                                # [asm 334D je / 334F-335A]
        right_keep = (0xFF >> cl) & 0xFF
        for p in range(4):
            planes[p][di] &= right_keep


@oracle_link("1030:31F4",
             "build one frame's scaled-column table for the end-level scale transition: "
             "for SCALE_COLUMNS source columns, scaled = (src*scale>>6)+offset; keep only "
             "columns whose scaled X is strictly decreasing AND below x_clamp. Returns "
             "(xs, ys) of the kept columns (the [0x6B14]/[0x6A88] tables + count bp).",
             "VERIFIED", merge_target="render_frame")
def build_scaled_columns(src_x, src_y, scale: int, x_off: int, y_off: int, x_clamp: int,
                         columns: int = SCALE_COLUMNS, running_init: int = 0x7D0):
    """Recover ``1030:31F4-3249`` — the per-frame iris-circle geometry.

    ``src_x``/``src_y`` are a **quarter-circle cos/sin table** (``[0x7090]``/``[0x6F90]``,
    one byte per column: ``src_x[i]=round(64·cos)``, ``src_y[i]=round(64·sin)``, so
    ``src_x²+src_y²≈64²``). ``scale`` is ``[0x2DD0]`` — the iris **radius** (only the low byte
    is used — ``mul byte``); it shrinks each frame. Each kept column is a point on the circle of
    that radius about ``(x_off, y_off)`` (``[0x2DC6]``/``[0x2DC8]`` — the player). The kept
    columns form a strictly-decreasing-X envelope clamped to ``x_clamp`` — one octant of the
    iris outline (``draw_scale_frame`` mirrors it to the full circle). Pure geometry; no VRAM.
    """
    xs: list[int] = []
    ys: list[int] = []
    running = running_init                                # [asm 31F6: [2DCC]=0x7D0]
    sc = scale & 0xFF                                     # [asm 320C: mul byte ptr [2DD0]]
    clamp = _s16(x_clamp)
    for i in range(columns):                              # [asm 3204..3249: bx=0..0x40]
        sx = _s16(((_s16((src_x[i] * sc) & 0xFFFF) >> 6) + x_off) & 0xFFFF)  # [asm 320C-3214]
        if sx >= running:                                # [asm 3218-321C: cmp / jge]
            continue
        if sx >= clamp:                                  # [asm 321E-3222: cmp / jge]
            continue
        running = sx                                     # [asm 3224: [2DCC]=ax]
        sy = _s16(((_s16((src_y[i] * sc) & 0xFFFF) >> 6) + y_off) & 0xFFFF)  # [asm 322B-3235]
        xs.append(sx)                                    # [asm 3227: [si+6B14]=ax]
        ys.append(sy)                                    # [asm 3239: [si+6A88]=ax]
    return xs, ys


@oracle_link("1030:324B",
             "one frame's clear pass of the circular IRIS transition: walk rows inward; per "
             "row clear everything OUTSIDE the iris circle via clear_span, using the circle's "
             "4-fold symmetry (the row's left+right spans and their mirror about (x_off,y_off) "
             "= the player) and the iris-circle column table. Writes the 4 EGA planes.",
             "VERIFIED", merge_target="render_frame")
def draw_scale_frame(planes, table_x, table_y, count: int, x_off: int, y_off: int,
                     x_clamp: int, page: int, stride: int = ROW_STRIDE):
    """Recover ``1030:324B-32AE`` — clear the borders exposed at this scale step.

    ``table_x``/``table_y`` are the raw ``[0x6B14]``/``[0x6A88]`` words (pass the live
    region, not just the first ``count`` entries: the loop reads ``table_x[si]`` every
    row and may step into stale tail entries exactly as the ASM does). The window is
    symmetric about ``(x_off, y_off)``; ``page`` is the destination CRTC page ``[0x2DD8]``.
    Caller has set SC map mask 0x0F (all planes) and reset the GC (1030:452B).

    Returns the terminal ``(cur_y, cur_x)`` — the values the ASM leaves in ``[0x2DCA]``
    /``[0x2DD2]`` at 32B0, so a live caller can write the DGROUP scratch back exactly.
    """
    cur_y = y_off                                  # [asm 324D: [2DCA]=[2DC8]]
    si = 0
    bp = count
    cur_x = x_clamp & 0xFFFF                        # [asm 3253: [2DD2]=[2DC4]]
    while True:
        if table_x[si] == cur_x:                   # [asm 3259-3261]
            bp -= 1                                 # [asm 3263]
            if bp < 0:                              # [asm 3264: js 32B0]
                break
            cur_y = table_y[si]                     # [asm 3266]
            si += 1                                 # [asm 326D]
        w_right = (0x140 - cur_y) & 0xFFFF          # [asm 3281: cx=0x140-bx]
        mrow = (2 * x_off - cur_x) & 0xFFFF         # [asm 328A-3291]
        w_left = (2 * y_off - cur_y) & 0xFFFF       # [asm 3296-329D]
        clear_span(planes, cur_y, w_right, cur_x, page, stride)   # [asm 3287]
        clear_span(planes, cur_y, w_right, mrow, page, stride)    # [asm 3293]
        clear_span(planes, 0, w_left, mrow, page, stride)         # [asm 32A1]
        clear_span(planes, 0, w_left, cur_x, page, stride)        # [asm 32A5]
        cur_x = (cur_x - 1) & 0xFFFF                # [asm 32A8: dec [2DD2]]
        if cur_x == 0:                              # [asm 32AC: je 32B0]
            break
    return cur_y & 0xFFFF, cur_x & 0xFFFF           # terminal [0x2DCA]/[0x2DD2]


@oracle_link("1030:6772",
             "one step of a linear DAC palette fade: 0x30 (16 colours × RGB) 6-bit "
             "components stepped from `a` toward `b` by `fade_amt`; returns (new 48-byte "
             "DAC palette, all_arrived). Caller swaps a/b for the reverse direction "
             "([0x6C02]) and stops (clears [0x6C01]/[0x6C02]) when all_arrived.",
             "VERIFIED", merge_target="render_frame")
def fade_palette(a: bytes, b: bytes, fade_amt: int) -> tuple[bytes, bool]:
    """Recover ``1030:6772`` — advance a DAC palette one fade step from ``a`` toward ``b``.

    Each of the ``DAC_COMPONENTS`` 6-bit components moves at most ``fade_amt`` toward the
    target; a component within ``fade_amt`` snaps to ``b``. Returns the new 48-byte 6-bit
    DAC palette and whether *every* component has arrived (the caller then ends the fade).

    ``a`` is the side being stepped (``lodsb`` source); the original swaps ``a``/``b`` via
    the direction flag ``[0x6C02]`` so the same routine runs the fade in either direction.
    """
    out = bytearray(DAC_COMPONENTS)
    all_arrived = True
    for i in range(DAC_COMPONENTS):
        ai, bi = a[i], b[i]
        diff = ai - bi                                # [asm 67AE: sub al,ah  (signed)]
        if abs(diff) <= fade_amt:                     # [asm 67B6: cmp al,dl / jbe -> snap]
            out[i] = bi                               # [asm 67B8: mov al,ah]
        else:                                         # [asm 67BB: al = orig - (±fade_amt)]
            out[i] = (ai - (fade_amt if diff >= 0 else -fade_amt)) & 0xFF
            all_arrived = False                       # [asm 67BF: inc bp  (not-arrived count)]
    return bytes(out), all_arrived
