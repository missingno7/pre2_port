"""The 1030:3922 scripted-camera-scroll state half (only LEVELG / index 0x0F carries a script)."""
from __future__ import annotations

from pre2.native.state import DATA_SEG, NativeGameState
from pre2.recovered.scroll_script import scroll_script_state

_BASE = DATA_SEG << 4


def _state(words):
    d = bytearray(0x100000)
    for off, val in words.items():
        d[_BASE + off] = val & 0xFF
        d[_BASE + off + 1] = (val >> 8) & 0xFF
    # the LEVELG script table @0x2d76: {thr, delta, clamp} x3 then -1
    for i, (thr, dl, cl) in enumerate(((0x01B8, 1, 40), (0x0370, 6, 100), (0x0528, 1, 255))):
        b = 0x2D76 + i * 6
        for off, v in ((b, thr), (b + 2, dl), (b + 4, cl)):
            d[_BASE + off] = v & 0xFF
            d[_BASE + off + 1] = (v >> 8) & 0xFF
    d[_BASE + 0x2D88] = 0xFF
    d[_BASE + 0x2D89] = 0xFF
    st = NativeGameState(d)
    return st, st.rb, st.rw


def test_counter_only_off_gate():
    st, rb, rw = _state({0x2DBE: 0x100, 0x6BF6: 0, 0x2DBC: 0x2D76, 0x6BD5: 0x101})   # &3 != 0
    w = scroll_script_state(rb, rw)
    assert w == {0x2DBE: 0x101}                       # only the counter ticks


def test_accumulate_and_clamp():
    st, rb, rw = _state({0x2DBE: 0x2AC, 0x6BF6: 0, 0x2DBC: 0x2D76, 0x6BD5: 0x2AC})    # past thr 0x1b8
    w = scroll_script_state(rb, rw)
    assert w[0x2DBE] == 0x2AD and w[0x6BF6] == 1       # +delta(1)
    st, rb, rw = _state({0x2DBE: 0x2AC, 0x6BF6: 40, 0x2DBC: 0x2D76, 0x6BD5: 0x2AC})   # already at clamp 40
    assert scroll_script_state(rb, rw)[0x6BF6] == 40   # stays clamped


def test_advance_to_next_entry():
    # counter past the SECOND threshold (0x370) -> the entry pointer advances by 6 for the NEXT frame; THIS frame
    # still uses entry 0's delta(1)/clamp(40), since bx was loaded before the advance.
    st, rb, rw = _state({0x2DBE: 0x370, 0x6BF6: 0x40, 0x2DBC: 0x2D76, 0x6BD5: 0x374})
    w = scroll_script_state(rb, rw)
    assert w[0x2DBC] == 0x2D7C and w[0x6BF6] == 40     # pointer advanced; scroll clamped to entry 0's 40
    st, rb, rw = _state({0x2DBE: 0x370, 0x6BF6: 0x40, 0x2DBC: 0x2D7C, 0x6BD5: 0x374})
    assert scroll_script_state(rb, rw)[0x6BF6] == 0x46  # next frame: entry 1 (delta 6, clamp 100) applies


def test_empty_script_is_counter_only():
    st, rb, rw = _state({0x2DBE: 5, 0x6BF6: 0, 0x2DBC: 0x2D88, 0x6BD5: 0})            # ptr -> the -1 marker
    assert scroll_script_state(rb, rw) == {0x2DBE: 6}
