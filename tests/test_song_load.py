"""Recovered MOD song-loader transforms (1030:2F4's three hot loops). Proven byte-exact vs the ASM end to end
(the whole 0x2F4 re-run with these short-circuiting the loops matched pure-ASM byte-for-byte); these pin the
two non-trivial transforms."""
from __future__ import annotations

from pre2.recovered.song_load import remap_pattern_periods, scale_samples


def test_scale_samples_is_signed_to_scaled_unsigned():
    # ((b + 0x80) & 0xFF) >> 2  — the +0x80 wraps at 8 bits before the >>2
    assert scale_samples(bytes([0x00, 0x7F, 0x80, 0xFF, 0x40])) == bytes([0x20, 0x3F, 0x00, 0x1F, 0x30])


def _cell(period: int, effect: int) -> bytes:
    return bytes([(period >> 8) & 0xFF, period & 0xFF, (effect >> 8) & 0xFF, effect & 0xFF])


def test_remap_periods_stores_1based_index_keeps_effect():
    table = [0x358, 0x328, 0x2FA]                       # 3 period-table entries
    cells = _cell(0x328, 0x0C00) + _cell(0x999, 0x0A05) + _cell(0x000, 0x0000)
    out = remap_pattern_periods(cells, table)
    assert out[0:2] == bytes([0x02, 0x00])              # 0x328 at table[1] -> 1-based index 2
    assert out[2:4] == bytes([0x0C, 0x00])              # effect word untouched
    assert out[4:6] == bytes([0x24, 0x00])              # 0x999 not found -> 0x24
    assert out[6:8] == bytes([0x0A, 0x05])              # effect word untouched
    assert out[8:12] == bytes([0, 0, 0, 0])             # zero period cell left as-is


def test_remap_masks_sample_number_high_nibble():
    # a real cell packs the sample-number high bits in b0's high nibble; the period is only b0&0x0F..b1
    table = [0x328]
    out = remap_pattern_periods(_cell(0x1328, 0x0000), table)   # 0x13,0x28 -> period 0x328
    assert out[0:2] == bytes([0x01, 0x00])              # found at table[0] -> 1-based index 1
