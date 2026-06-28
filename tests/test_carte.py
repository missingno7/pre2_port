"""Byte-exact regression for the recovered CARTE (world-map) scroll-in background (1030:9613..96AB).

`pre2.recovered.carte.build_carte_page` composes the carte map page as a pure function of the
horizontal scroll: a black 0x2000 circular page filled column-by-column from the decoded map asset
(segment [0x2875]) by the recovered `scroll_blit_column` (965A) as the CRTC pan reveals it.

Goldens captured from the original ASM under the VM (driving snapshot_pre2_20260624_210538 into the
carte scroll-in): SHA-256 of the four EGA planes (0x2000 each) the VM had built at each scroll_x. The
scroll range covers the ring wrap past 320 px (scroll_x > 320). Live lockstep over the whole scroll
confirmed diff=0 at scroll 8/16/64/128/200/256/300/400/500/639; this is the fast committed check.

Carte enters on a cleared (black) page — captured ground truth: at scroll_x=8 the page is all-zero.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pre2.recovered.carte import build_carte_page

_FIX = Path(__file__).parent / "fixtures" / "carte"
_ASSET = _FIX / "carte_asset.bin"
_GOLD = _FIX / "carte_page_goldens.json"


def test_carte_page_byte_exact_vs_asm():
    asset = _ASSET.read_bytes()
    goldens = json.loads(_GOLD.read_text())
    assert goldens, "empty carte goldens"
    for scroll_str, want in sorted(goldens.items(), key=lambda kv: int(kv[0])):
        scroll_x = int(scroll_str)
        planes = build_carte_page(asset, scroll_x)
        got = hashlib.sha256(b"".join(bytes(p) for p in planes)).hexdigest()
        assert got == want, f"carte page mismatch at scroll_x={scroll_x}: {got} != {want}"


def test_carte_enters_on_black_page():
    # At the first scroll iteration (scroll_x = 8) no column has been blitted yet -> black ring.
    asset = _ASSET.read_bytes()
    planes = build_carte_page(asset, 8)
    assert all(not any(p) for p in planes), "carte page must start black at scroll_x=8"


def test_carte_312px_hides_pel_pan_incoming_column():
    # The carte narrows the CRTC active area to 312px so the pel-pan's incoming byte-column scrolls in
    # from the OVERSCAN/border (off-screen), not the active area — that is what makes the fine scroll
    # fluent. With pel>0 the present must drop the last `pel` active pixels (the un-fetched next byte),
    # for the narrowed width only. Regression guard: the clamp for this was added then reverted once,
    # and the 312px clip alone still left the column at px 306-311 visible.
    import sys
    import numpy as np
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    from sdl_view import _planar_to_rgb, _PLANAR_ROW_BYTES
    planes = [np.zeros(0x10000, dtype=np.uint8) for _ in range(4)]
    planes[0][_PLANAR_ROW_BYTES] = 0xFF          # byte 39 = the incoming overflow column -> colour index 1
    pal = [(0, 0, 0), (255, 0, 0)] + [(0, 0, 0)] * 14
    carte = _planar_to_rgb(lambda p: planes[p], 0, pal, 0xFFFF, pel_pan=4, active_width=312)
    assert not (carte[0] == (255, 0, 0)).all(axis=-1).any(), "carte: incoming column leaked into the active area"
    # the full-width gameplay path is unchanged — that same column is the normal next scroll column
    game = _planar_to_rgb(lambda p: planes[p], 0, pal, 0xFFFF, pel_pan=4, active_width=320)
    assert (game[0] == (255, 0, 0)).all(axis=-1).any()
