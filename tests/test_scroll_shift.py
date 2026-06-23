"""Regression for the recovered menu/scene framebuffer scroll (1030:9804..9876).

`scroll_shift_frame` is the 4-plane A000 self-copy that shifts the displayed buffer to
follow the camera (the mode-select's hottest op). The byte-exact-vs-ASM proof is the in-VM
lockstep (pre2/probes/verify_scroll_shift.py: 40 frames, both parts, 0 divergence on snapshot
075918); this is the deterministic guard on the shift arithmetic (synthetic planes).
"""
from __future__ import annotations

import hashlib

from pre2.recovered.present import scroll_shift_frame


def _planes():
    return [bytearray((i + p * 7) & 0xFF for i in range(0x10000)) for p in range(4)]


def _hash(planes):
    return hashlib.sha256(b"".join(planes)).hexdigest()[:16]


def test_scroll_shift_down_with_horizontal():
    planes = _planes()
    # b199 bit3=0 vs scroll_x bit3=1 -> Part 1 fires; scroll_y delta +3 -> Part 2 scrolls down
    scroll_shift_frame(planes, b199=0, scroll_x=8, scroll_y=20, prev_scroll_y=17,
                       page_draw=0x1000, wrap=0x1FFF)
    assert _hash(planes) == "7462efd188b1e8e5"


def test_scroll_shift_up_only():
    planes = _planes()
    # b199 & scroll_x share bit3 -> no Part 1; delta -4 -> Part 2 scrolls up
    scroll_shift_frame(planes, b199=8, scroll_x=8, scroll_y=10, prev_scroll_y=14,
                       page_draw=0x1000, wrap=0x1FFF)
    assert _hash(planes) == "34714c31a329e39a"


def test_scroll_shift_noop_when_no_motion():
    planes = _planes()
    orig = [bytes(p) for p in planes]
    scroll_shift_frame(planes, b199=8, scroll_x=8, scroll_y=10, prev_scroll_y=10,
                       page_draw=0x1000, wrap=0x1FFF)   # no boundary cross, zero delta
    assert all(bytes(planes[p]) == orig[p] for p in range(4))
