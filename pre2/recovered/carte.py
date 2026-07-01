"""The recovered CARTE (world-map) scene background — composed straight from the decoded asset.

The carte/map "scroll-in" (the ``1030:9600`` loop, ``9613..96AB``) reveals the map by panning the
CRTC display start across a ``0x2000``-byte circular page while filling the newly-exposed column each
8 px from the master map asset (segment ``[0x2875]``, 640 px / 80 bytes wide, 4 EGA planes at source
offsets 0 / 0x3E80 / 0x7D00 / 0xBB80). The page starts **black** (the carte enters on a cleared page —
captured ground truth: at the first scroll iteration ``scroll_x=8`` the page is 0 nonzero bytes in all
four planes) and the map scrolls in from black, column by column.

The decisive property (proven byte-exact vs the VM page at scroll 8 / 16 / 64 / 128 / 200 / 256 / 300 /
400 / 500 / 639 — covering the ring wrap past 320 px): **the page is a pure, stateless function of
``scroll_x``** — there is no real history dependence. ``scroll_blit_column`` blits asset -> page (not a
VRAM self-copy), so the whole page is reconstructible by replaying the blits from the black ring:

    page(scroll_x) = black 0x2000 ring + scroll_blit_column(asset, k) for k in [scroll_start, scroll_x)

This is the carte counterpart of :func:`pre2.recovered.gameover_background.render_gameover_background`
(both compose a scene background from the decoded asset, no VM framebuffer). It dissolves the
"history-dependent buffer" blocker for the carte (the MENU stays blocked: it scrolls via
``scroll_shift_frame`` — an A000->A000 self-copy that IS genuinely stateful).

Pure: no ``cpu``/``mem``/``dos_re`` imports. The VM<->memory translation belongs in ``pre2/bridge/``.
"""
from __future__ import annotations

from typing import List

from pre2.islands import oracle_link
from pre2.recovered.present import (PAGE_WRAP, compute_display_start, pixel_pan,
                                    scroll_blit_column)

PAGE_LEN = PAGE_WRAP + 1          # 0x2000 — the circular page
SCROLL_START = 8                  # [asm 95E2: mov [0xB19D],8] — the carte begins at scroll_x = 8


@oracle_link("1030:9613",
             "the carte/map scroll-in background: a black 0x2000 circular page filled column-by-column "
             "from the map asset (segment [0x2875]) by scroll_blit_column (965A) as the CRTC pan reveals "
             "it. Proven a PURE function of scroll_x (page = black ring + replay scroll_blit_column over "
             "[8, scroll_x)); byte-exact vs the VM page across the whole scroll (8..639, incl. ring wrap). "
             "Carte enters on a cleared (black) page — there is no separate initial full-page-fill "
             "producer.",
             "VERIFIED", merge_target="render_scene")
def build_carte_page(asset: bytes, scroll_x: int, scroll_start: int = SCROLL_START) -> List[bytearray]:
    """Compose the carte map page for a given horizontal scroll position.

    ``asset`` is the decoded map master (segment ``[0x2875]`` bytes: 4 planes at offsets
    0 / 0x3E80 / 0x7D00 / 0xBB80, 80-byte source rows). ``scroll_x`` is the current pan (``[0xB19D]``).
    Returns the four EGA plane buffers (``0x2000`` each) the carte loop has built up by the time the
    scroll reaches ``scroll_x`` — i.e. the blits for ``k`` in ``[scroll_start, scroll_x)`` applied to a
    black ring (carte is horizontal-only, ``scroll_y`` = 0). Deplanarize with
    :func:`compute_display_start` / :func:`pixel_pan` for the on-screen view.
    """
    planes = [bytearray(PAGE_LEN) for _ in range(4)]
    for k in range(scroll_start, scroll_x & 0xFFFF):
        scroll_blit_column(planes, asset, k)        # no-op unless k & 7 == 0 [asm 9662]
    return planes


@oracle_link("1030:9543",
             "the carte 'you are here' marker de-planarization (9543..95CD): stamps the player-position "
             "sprite from [0x667a]:[0x62da] (mask + 4 colour planes, each w*h bytes, consecutive) onto the "
             "map master at di = ((y-0x20)&0xFF)*0x50 + (x>>3), where (x,y) = the per-level position word "
             "pair [0xB148 + level*4] / [0xB14A + level*4] and (w,h) = ([0x7522]&0xFF)>>3 / [0x7522]>>8. "
             "AND-mask then OR-plane, plane offsets 0/0x3E80/0x7D00/0xBB80 (the 4 map-asset planes), di "
             "advances +1 per column and +(0x50-w) per row. Proven byte-exact vs the VM stamped master "
             "(the 414-byte 0.6% overlay over MAP.SQZ).",
             "VERIFIED", merge_target="render_scene")
def stamp_carte_marker(asset: bytes, marker: bytes, di: int, w: int, h: int) -> bytes:
    """Stamp the carte's per-level 'you are here' marker onto the decoded map master.

    ``asset`` is the raw map master (``[0x2875]`` bytes, the same 0xFA00 blob :func:`build_carte_page`
    consumes). ``marker`` is the ``5*w*h``-byte source at ``[0x667a]:[0x62da]`` — a mask plane followed by
    four colour planes, each ``w*h`` bytes. ``di`` / ``w`` / ``h`` come from the per-level position table
    and the marker dimensions (see the ``[asm]`` note). Returns a new master with the marker composited in
    (``dst = (dst & ~mask) | colour`` on each of the 4 planes at offsets 0/0x3E80/0x7D00/0xBB80).
    """
    PLANES = (0x0000, 0x3E80, 0x7D00, 0xBB80)                   # [asm 958B-9598] the 4 map-asset plane bases
    wh = w * h
    buf = bytearray(0x10000)                                    # a full 64K segment (di+plane may wrap the word)
    buf[:len(asset)] = asset
    lin = 0
    d = di & 0xFFFF
    for _row in range(h):                                       # [asm 95C9-95CB] dh rows
        for _col in range(w):                                   # [asm 95C5] cx=w byte columns
            mask = marker[lin]                                  # [asm 9587] mask byte
            inv = (~mask) & 0xFF                                # [asm 9589] not al
            for p in range(4):
                off = (d + PLANES[p]) & 0xFFFF
                buf[off] = (buf[off] & inv) | marker[(p + 1) * wh + lin]  # [asm 958B..95B8] and mask, or colour
            lin += 1                                            # [asm 95C3] inc si (source runs linearly)
            d = (d + 1) & 0xFFFF                                # [asm 95C4] inc di
        d = (d + (0x50 - w)) & 0xFFFF                           # [asm 95C7] di += 0x50 - w (next row)
    return bytes(buf[:len(asset)])


def carte_marker_offset(x_word: int, y_word: int) -> int:
    """The map byte-offset ``di`` of the per-level marker: ``((y-0x20)&0xFF)*0x50 + (x>>3)`` [asm 954D-9560]."""
    return (((y_word - 0x20) & 0xFF) * 0x50 + (x_word >> 3)) & 0xFFFF


def carte_display(scroll_x: int, scroll_y: int = 0):
    """The on-screen view parameters for the carte at ``scroll_x``: ``(display_start, pel_pan)``.

    ``display_start`` is the CRTC start address (the 0x2000-wrapped pan); ``pel_pan`` the sub-byte
    horizontal pixel pan written to the attribute controller. Both are the already-recovered scene
    present leaves (``9613`` / ``9654``)."""
    return compute_display_start(scroll_x, scroll_y), pixel_pan(scroll_x)
