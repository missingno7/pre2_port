"""Fast regression for the recovered camera-shake apply (1030:4C30).

The function is proven byte-exact vs the ASM live in pre2/probes/verify_camera_shake_live.py
(123 applies, 0 divergences, magnitudes 0..8, both parities, over the full landing-shake decay).
This locks that recovered behaviour against edits without the VM: the renderer-visible row-stride
bias [0x6BF8] alternates {0, magnitude+1} by frame parity, magnitude 0 leaves it unchanged,
magnitude 1 writes 0, and the odd-frame horizontal nudge [0x4F1E]-=3 is skipped for [0x4F27] in
{5,0x20}.
"""
from __future__ import annotations

from pre2.recovered.camera_shake import apply_camera_shake


def test_magnitude_zero_leaves_row_factor_unchanged():
    r = apply_camera_shake(row_factor_in=0x1234, magnitude=0, parity=1, f27=0, h_scroll_in=0x80)
    assert (r.row_factor, r.magnitude, r.h_scroll) == (0x1234, 0, 0x80)  # no writes


def test_magnitude_one_writes_zero():
    for parity in (0, 1):
        r = apply_camera_shake(row_factor_in=0x99, magnitude=1, parity=parity, f27=0, h_scroll_in=0x80)
        assert (r.row_factor, r.magnitude, r.h_scroll) == (0, 1, 0x80)


def test_even_parity_zero_odd_parity_magnitude_plus_one():
    even = apply_camera_shake(row_factor_in=0x99, magnitude=5, parity=0, f27=0, h_scroll_in=0x80)
    assert even.row_factor == 0 and even.magnitude == 5            # even -> 0, magnitude untouched
    odd = apply_camera_shake(row_factor_in=0x99, magnitude=5, parity=1, f27=0, h_scroll_in=0x80)
    assert odd.row_factor == 6 and odd.magnitude == 6             # odd -> magnitude+1, jitter +1


def test_odd_frame_horizontal_nudge_and_its_skip():
    nudged = apply_camera_shake(row_factor_in=0, magnitude=7, parity=1, f27=0, h_scroll_in=0x80)
    assert nudged.h_scroll == 0x80 - 3                           # [0x4F1E]-=3 on the odd frame
    for f27 in (5, 0x20):                                        # skipped in these states
        skip = apply_camera_shake(row_factor_in=0, magnitude=7, parity=1, f27=f27, h_scroll_in=0x80)
        assert skip.h_scroll == 0x80 and skip.row_factor == 8


def test_h_scroll_wraps_word():
    r = apply_camera_shake(row_factor_in=0, magnitude=7, parity=1, f27=0, h_scroll_in=0x0001)
    assert r.h_scroll == (0x0001 - 3) & 0xFFFF                   # 0xFFFE
