"""The recovered mode-select MENU scene buffer — the stateful persistent page (1030:96D5 controller).

Unlike the carte ([[pre2.recovered.carte]]), the menu page is NOT a pure function of the scroll
counter: the menu loop (97A8..987E) never blits fresh columns from the asset — it pans by physically
self-copying VRAM. So the page evolves statefully:

  * **seed** (the 9718 ``rep movsw`` initial fill): A000 planes 0,1 = the bg pattern from ``[0x2875]``
    (plane0 = ``[0x2875]``:0..0x1F40, plane1 = ``[0x2875]``:0x1F40..0x3E80); planes 2,3 start black.
  * **per frame** the menu controller runs, in order: ``present_pan_flip`` (CRTC pan + page flip) ->
    the ``[0xB1AE]`` callback (the menu state machine: ``draw_string`` x2 stamps the MODE / BEGINNER-
    EXPERT lines into planes 2|3 of the current draw page) -> ``scroll_shift_frame`` (9804, the 4-plane
    A000->A000 latched self-copy that pans the whole buffer to follow the bouncing camera).

This class **owns** that evolving page and applies the already-recovered+verified rendering leaves
(:func:`~pre2.recovered.text.draw_string`, :func:`~pre2.recovered.present.scroll_shift_frame`) to its
own clean plane buffers — it never reads VM VRAM. The bridge/checkpoint drives it from the SAME leaf-call
events the live ASM path performs (the original runtime is the authoritative event producer); FaithfulVisual
is a pure CONSUMER of :attr:`planes` (it does not own this state). If a menu frame is reached without a
prior seed (e.g. a mid-menu snapshot attach), the consumer must fail with a menu-specific gap — there is no
VM-framebuffer fallback.

Pure: no ``cpu``/``mem``/``dos_re`` imports.
"""
from __future__ import annotations

from typing import List

from pre2.islands import oracle_link
from pre2.recovered.present import PAGE_WRAP, scroll_shift_frame
from pre2.recovered.text import draw_string

PLANE_LEN = 0x10000          # mirror the VM EGA plane: the leaves wrap row starts at 0x1FFF but a glyph
                             # cell's 3 bytes can spill a few bytes past 0x2000 (the VM plane is 0x10000)
_BG_PLANE = 0x1F40           # bytes per bg plane in the initial fill [asm 9718: rep movsw cx=0xFA0]
_FONT_SRC = 0x1FE0           # [asm 9736] the seed reads the glyph rows from font[0x1FE0..]
_FONT_WORDS = 0x560          # [asm 9739] words per shift copy
_SHIFT_COPY = 0x1080         # bytes per shift copy (0x560*3 + 0x60 pad) — draw_string's font_base step


@oracle_link("1030:972E",
             "the menu's bit-rotated font (the 96D5 seed's 2nd pass): 8 sub-pixel-shifted copies of the font "
             "glyphs from [0x3d]:0x1FE0, each 0x1080 bytes, selected by draw_string's font_base=(scroll_x&7)*"
             "0x1080. Each 16-bit glyph row (read big-endian) is shifted right by the copy index k, the k "
             "shifted-out bits captured in a 3rd byte, so the menu text scrolls smoothly with the map.",
             "VERIFIED", merge_target="render_scene")
def build_shifted_font(font_seg: bytes) -> bytes:
    """[asm 972E-9765] Build the menu's 8-shift font from the raw font segment (``[0x3d]``).

    ``font_seg`` is the font segment's bytes (at least ``0x1FE0 + 0x560*2`` long). Returns the 0x8400-byte
    bit-rotated font: 8 copies (shift 0..7), each ``0x1080`` bytes, that :func:`~pre2.recovered.text.draw_string`
    indexes with ``font_base = (scroll_x & 7) * 0x1080``. Every glyph row is a 16-bit value read big-endian
    (leftmost pixel = MSB), shifted right ``k`` bits for copy ``k`` (the overflow bits land in a trailing 3rd
    byte), giving the sub-pixel horizontal offset the scrolling text needs."""
    out = bytearray()
    for shift in range(8):                                      # [asm 9732/975E dh = 0..7]
        si = _FONT_SRC
        for _ in range(_FONT_WORDS):                            # [asm 973C lodsw]
            ax = font_seg[si] | (font_seg[si + 1] << 8)
            ax = ((ax & 0xFF) << 8) | (ax >> 8)                # [asm 973D xchg ah,al -> big-endian]
            carry = 0
            for _ in range(shift):                             # [asm 9747 shr ax,1 ; rcr dl,1]
                bit = ax & 1
                ax >>= 1
                carry = ((carry >> 1) | (bit << 7)) & 0xFF
            ax = ((ax & 0xFF) << 8) | (ax >> 8)                # [asm 974F xchg ah,al -> little-endian]
            out.append(ax & 0xFF)                              # [asm 9751 stosw]
            out.append((ax >> 8) & 0xFF)
            out.append(carry)                                  # [asm 9754 stosb]
            si += 2
        out.extend(b"\x00" * 0x60)                             # [asm 9757 pad: cx=0x30 words]
    return bytes(out)


@oracle_link("1030:96D5",
             "the mode-select MENU persistent page: seed planes 0,1 from the bg asset [0x2875] (the 9718 "
             "rep movsw fill; planes 2,3 black), then evolve it each frame by the recovered leaves the "
             "controller runs — draw_string (the MODE/BEGINNER-EXPERT text into planes 2|3) + "
             "scroll_shift_frame (the 4-plane A000->A000 pan). A STATEFUL page (NOT a pure fn of scroll_x "
             "like the carte); owned here, driven by the runtime's leaf-call events, consumed by FaithfulVisual.",
             "VERIFIED", merge_target="render_scene")
class MenuScenePage:
    """Stateful owner of the mode-select menu's evolving 4-plane page."""

    def __init__(self) -> None:
        self.planes: List[bytearray] = [bytearray(PLANE_LEN) for _ in range(4)]
        self.seeded = False

    def seed(self, asset: bytes) -> None:
        """The 9718 initial fill: planes 0,1 = the bg pattern from ``[0x2875]``; planes 2,3 black."""
        self.planes = [bytearray(PLANE_LEN) for _ in range(4)]
        self.planes[0][0:_BG_PLANE] = asset[0:_BG_PLANE]
        self.planes[1][0:_BG_PLANE] = asset[_BG_PLANE:2 * _BG_PLANE]
        self.seeded = True

    def stamp_text(self, text: bytes, font: bytes, font_base: int, pen: int, advance: int,
                   page_draw: int, page_clear: int) -> int:
        """Apply one ``draw_string`` event (a MODE/BEGINNER-EXPERT line) into planes 2|3. Returns the pen."""
        return draw_string(self.planes, text, font, font_base, pen, advance, page_draw, page_clear)

    def scroll_shift(self, b199: int, scroll_x: int, scroll_y: int, prev_scroll_y: int,
                     page_draw: int, wrap: int = PAGE_WRAP) -> None:
        """Apply one ``scroll_shift_frame`` event (the 4-plane A000->A000 pan) to the owned page."""
        scroll_shift_frame(self.planes, b199, scroll_x, scroll_y, prev_scroll_y, page_draw, wrap=wrap)
