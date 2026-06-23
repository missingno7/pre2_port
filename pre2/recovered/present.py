"""Prehistorik 2 scene present — recovered logic (pure).

The non-gameplay scenes (the mode-select scroll in particular) present by **panning the
CRTC display start** and **flipping the draw/clear page offsets**, rather than by the
gameplay scroll engine. This recovers that present arithmetic from the mode-select loop
``1030:9600`` (``9613..9639``), one bounded computation:

* :func:`compute_display_start` — the CRTC start address (the pan): ``(scroll_x>>3 +
  scroll_y*0x28) & 0x1FFF``. The ``& 0x1FFF`` (``and bh,0x1f``) makes the display a
  ``0x2000``-byte **circular page** — the background wraps (repeats) at the page edge.
* :func:`present_pan_flip` — one present step: compute the display start, then set the
  page bookkeeping the text drawer uses. The original does ``xchg [0xB1A1],bx`` (``bx`` =
  the new display start) then ``[0xB1A3]=bx`` (the old draw page), so **``page_draw`` becomes
  the just-shown display page** and ``page_clear`` becomes the previous draw page. (So the
  menu draws text into the *visible* page — there is no hidden back-buffer for the text,
  which is why a redraw can be observed mid-glyph.)
* :func:`pixel_pan` — the sub-byte horizontal pan (``scroll_x & 7``) written to the
  attribute controller's horizontal-pixel-panning register (``[asm 9654-9659]``).

Pure: no ``cpu``/``mem``/``dos_re`` imports. The VM↔memory translation belongs in
``pre2/bridge/``.
"""
from __future__ import annotations

from pre2.islands import oracle_link

__all__ = ["ROW_STRIDE", "PAGE_WRAP", "SRC_STRIDE", "SCROLL_ROWS",
           "compute_display_start", "present_pan_flip", "pixel_pan", "scroll_blit_column",
           "scroll_shift_frame"]

ROW_STRIDE = 0x28       # screen bytes per row [asm 9613: ax = 0x28 * scroll_y]
PAGE_WRAP = 0x1FFF      # 0x2000-byte circular page [asm 9626: and bh,0x1f]
SRC_STRIDE = 0x50       # master-pattern row stride (movsb +1, then add si,0x4f) [asm 9685]
SCROLL_ROWS = 0xC8      # 200 rows blitted per plane [asm 967F: cx=0xC8]


@oracle_link("1030:9613",
             "the mode-select CRTC pan: display_start = (scroll_x>>3 + scroll_y*0x28) "
             "& 0x1FFF — the 0x2000-byte circular page that makes the scrolling background "
             "wrap at the page edge.",
             "VERIFIED", merge_target="render_scene")
def compute_display_start(scroll_x: int, scroll_y: int,
                          stride: int = ROW_STRIDE, wrap: int = PAGE_WRAP) -> int:
    """Recover ``1030:9613-9626`` — the CRTC display-start (pan) for the scrolling scene.

    ``scroll_x`` (``[0xB19D]``) is the horizontal pan in pixels; ``scroll_y`` (``[0xB19F]``)
    the vertical offset in rows. The high byte is masked to ``0x1f`` (``& 0x1FFF``), so the
    display wraps as a ``0x2000``-byte circular page.
    """
    base = ((scroll_x >> 3) + (scroll_y * stride)) & 0xFFFF      # [asm 9613-9624]
    return base & wrap                                           # [asm 9626: and bh,0x1f]


@oracle_link("1030:9635",
             "one mode-select present step: compute the CRTC display start, then set the "
             "page bookkeeping — page_draw = the new display start (the visible page), "
             "page_clear = the previous page_draw (xchg [0xB1A1],bx ; [0xB1A3]=bx).",
             "VERIFIED", merge_target="render_scene")
def present_pan_flip(scroll_x: int, scroll_y: int, old_page_draw: int,
                     stride: int = ROW_STRIDE, wrap: int = PAGE_WRAP):
    """Recover ``1030:9613-9639`` — pan + page flip for one scene present step.

    Returns ``(display_start, page_draw, page_clear)``: ``display_start`` is written to the
    CRTC start address; ``page_draw`` (``[0xB1A1]``) becomes that display start; ``page_clear``
    (``[0xB1A3]``) becomes the previous ``page_draw``. The text drawer then draws into
    ``page_draw`` — i.e. the page now on screen.
    """
    display_start = compute_display_start(scroll_x, scroll_y, stride, wrap)
    return display_start, display_start, old_page_draw & 0xFFFF


@oracle_link("1030:9654",
             "the sub-byte horizontal pan: scroll_x & 7 -> attribute controller "
             "horizontal-pixel-panning register.",
             "ASM_MATCHED", merge_target="render_scene")
def pixel_pan(scroll_x: int) -> int:
    """Recover ``1030:9654-9659`` — the fine (sub-byte) horizontal pan, ``scroll_x & 7``."""
    return scroll_x & 7


@oracle_link("1030:965A",
             "the mode-select scrolling-background generator: every 8 px of pan, blit one "
             "fresh byte-column of the master pattern (segment [0x2875]) into all 4 EGA "
             "planes of the page (di wraps at 0x1FFF — the circular page), feeding the "
             "infinite horizontal scroll the CRTC pan reveals.",
             "VERIFIED", merge_target="render_scene")
def scroll_blit_column(planes, source, scroll_x,
                       wrap: int = PAGE_WRAP, src_stride: int = SRC_STRIDE,
                       dst_stride: int = ROW_STRIDE, rows: int = SCROLL_ROWS) -> None:
    """Recover ``1030:965A-969C`` — blit the newly-exposed background column.

    As the CRTC display start pans right (one byte every 8 px), the column it newly
    exposes must be filled with the next slice of the master pattern. This runs once per
    new byte-column (``scroll_x & 7 == 0``): for each of the 4 EGA planes it copies 200
    rows of one byte from the master ``source`` (segment ``[0x2875]``; row stride
    ``src_stride``) into page column ``(scroll_x>>3) - 1`` (``di`` advanced ``dst_stride``
    per row and wrapped to the ``0x2000`` circular page). ``planes`` is the four EGA plane
    buffers; ``source`` is the master-pattern segment bytes.
    """
    if scroll_x & 7:                                    # [asm 9662: test dl,7 / jne 969c]
        return
    col = ((scroll_x >> 3) - 1) & 0xFFFF                # [asm 966E-9674]
    si = col                                            # [asm 9675]
    di = col                                            # [asm 9677]
    for plane in range(4):                              # [asm 967C-9695: al = 1,2,4,8]
        for _r in range(rows):                          # [asm 967F: cx=0xC8]
            di &= wrap                                  # [asm 9682: and di,bp]
            planes[plane][di] = source[si]              # [asm 9684: movsb es:[di] <- ds:[si]]
            si = (si + 1 + (src_stride - 1)) & 0xFFFF   # movsb si++ then [asm 9685: add si,0x4f]
            di = (di + 1 + (dst_stride - 1)) & 0xFFFF   # movsb di++ then [asm 9688: add di,0x27]
        di = (di - 0x1F40) & 0xFFFF                     # [asm 968D: sub di,0x1f40]


@oracle_link("1030:9804",
             "the menu/scene framebuffer SCROLL: a 4-plane A000->A000 latched self-copy that "
             "shifts the displayed buffer to follow the camera. Part 1 (on an 8-px horizontal "
             "boundary cross) shifts the page_draw+0x27 column up one row over 200 rows; Part 2 "
             "shifts the buffer vertically by the scroll_y delta (up or down). di/si wrap at the "
             "0x2000 circular page. The enhanced renderer pans a camera instead of shifting VRAM.",
             "VERIFIED", merge_target="render_scene")
def scroll_shift_frame(planes, b199, scroll_x, scroll_y, prev_scroll_y, page_draw,
                       wrap: int = PAGE_WRAP) -> None:
    """Recover ``1030:9804-9876`` — the menu/scene framebuffer scroll (the hottest menu op).

    ``planes`` is the four EGA plane buffers (the A000 self-copy moves all four — an EGA
    latched copy). ``wrap`` is the page mask (``bp``, normally 0x1FFF). Faithful planar
    scroll; the semantic intent is just the camera moving by the scroll delta.
    """
    # Part 1: 8-px horizontal boundary -> shift the page_draw+0x27 column up one row [9804-9834]
    if (b199 & 8) != (scroll_x & 8):                    # [asm 9804-9810]
        si = (page_draw + 0x27) & 0xFFFF                # [asm 9812-9816]
        di = (si - 0x28) & 0xFFFF                       # [asm 9819]
        for _ in range(0xC8):                           # [asm 981C: cx=0xC8]
            si &= wrap                                  # [asm 9824]
            di &= wrap                                  # [asm 9826]
            for p in range(4):                          # [asm 9828: movsb (4-plane latched)]
                planes[p][di] = planes[p][si]
            si = (si + 0x28) & 0xFFFF                   # movsb +1 then [asm 9829: add si,0x27]
            di = (di + 0x28) & 0xFFFF                   # movsb +1 then [asm 982C: add di,0x27]

    # Part 2: vertical scroll by the scroll_y delta [9836-9876]
    delta = (scroll_y - prev_scroll_y) & 0xFFFF         # [asm 9836-983A]
    delta = delta - 0x10000 if delta & 0x8000 else delta
    if delta == 0:                                      # [asm 983E: je]
        return
    if delta > 0:                                       # [asm 9840 jns -> 9842] scroll down
        si = (page_draw - 1) & 0xFFFF                   # [asm 9842-9846]
        di = (si + 0x1F18) & 0xFFFF                     # [asm 9847]
        n = delta
    else:                                               # [asm 984D] scroll up
        n = -delta                                      # [asm 984D: neg dx]
        si = ((page_draw - 1) - 0x28 * n) & 0xFFFF      # [asm 984F-9858: si -= 0x28*|d|]
        di = si                                         # [asm 985A]
        si = (si + 0x1F18) & 0xFFFF                     # [asm 985C]
    count = (0x29 * n) & 0xFFFF                         # [asm 9865-9869: cx = 0x29*|d|]
    for _ in range(count):                              # [asm 986B-9870]
        si &= wrap
        di &= wrap
        for p in range(4):                              # movsb (4-plane latched)
            planes[p][di] = planes[p][si]
        si = (si + 1) & 0xFFFF
        di = (di + 1) & 0xFFFF
