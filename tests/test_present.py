"""Byte-exact regression for the recovered scene present (1030:9613..9639).

`present_pan_flip` / `compute_display_start` — the mode-select CRTC pan + page flip.
Golden fixture captured from the original ASM under the VM (the menu scroll in
`demo_pre2_20260622_192206`): for real (scroll_x, scroll_y, old_page_draw) inputs, the
display start the ASM wrote to the CRTC (`mem.ega_display_start`) plus the resulting
`[0xB1A1]`/`[0xB1A3]` page offsets. In-VM lockstep over the live menu scroll confirmed
321/321 present steps, 0 divergence; this is the fast committed check.

Note: every captured step has `scroll_y == 0` (the menu pans horizontally), so the
`scroll_y*0x28` term of the display-start formula is disasm-matched but not runtime
exercised — a synthetic case guards the arithmetic shape.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pre2.recovered.present import (compute_display_start, pixel_pan, present_pan_flip,
                                    scroll_blit_column)

_FIX = Path(__file__).parent / "fixtures" / "present" / "pan_flip.json"


def test_present_pan_flip_byte_exact_vs_asm():
    cases = json.loads(_FIX.read_text())["cases"]
    assert cases, "empty present fixture"
    for c in cases:
        ds, pd, pc = present_pan_flip(c["scroll_x"], c["scroll_y"], c["old_page_draw"])
        assert ds == c["display_start"], f"display_start: {c}"
        assert pd == c["page_draw"], f"page_draw: {c}"
        assert pc == c["page_clear"], f"page_clear: {c}"
        # page_draw is always the (new) display start; page_clear is the previous draw page
        assert pd == ds and pc == c["old_page_draw"]


def test_compute_display_start_formula():
    # the horizontal pan (scroll_y == 0): display_start = (scroll_x >> 3) & 0x1FFF
    assert compute_display_start(8, 0) == 1
    assert compute_display_start(0x26, 0) == 4
    # the page wraps as a 0x2000-byte circular buffer
    assert compute_display_start(0x2000 * 8, 0) == 0
    # the scroll_y*0x28 term (disasm-matched; not exercised by the menu where scroll_y==0)
    assert compute_display_start(0, 3) == (3 * 0x28)
    assert compute_display_start(0x10, 1) == (0x28 + 2)


def test_pixel_pan():
    assert pixel_pan(0) == 0
    assert pixel_pan(0x27) == 7
    assert pixel_pan(0x28) == 0


def test_scroll_blit_column_addressing():
    """The background scroll-blit's plane/column addressing (1030:965A..969C). The
    byte-exact-vs-ASM proof is the in-VM lockstep (pre2/probes/verify_scroll_blit.py:
    79 blits / 553 skips / 0 divergence); this is the deterministic regression guard on
    the recovered arithmetic, with a synthetic master pattern ``source[i] = i & 0xFF``."""
    source = bytes(i & 0xFF for i in range(0x10000))
    planes = [bytearray(0x10000) for _ in range(4)]
    scroll_blit_column(planes, source, 0x80)            # scroll_x=0x80 -> column 15
    # one fresh byte-column (200 rows) blitted into each of the 4 planes
    assert all(sum(1 for k in range(0x2000) if planes[p][k]) == 200 for p in range(4))
    assert planes[0][15] == source[15]                  # row 0 at di=col=15
    assert planes[0][(15 + 0x28) & 0x1FFF] == source[(15 + 0x50) & 0xFFFF]  # row 1
    assert hashlib.sha256(b"".join(planes)).hexdigest()[:16] == "947bd8f7a9c882c7"


def test_scroll_blit_column_skips_mid_byte():
    """No blit unless the pan crossed a byte boundary (scroll_x & 7 == 0)."""
    source = bytes(i & 0xFF for i in range(0x10000))
    planes = [bytearray(0x10000) for _ in range(4)]
    scroll_blit_column(planes, source, 0x83)            # 0x83 & 7 = 3 -> skip
    assert all(not any(p) for p in planes)
