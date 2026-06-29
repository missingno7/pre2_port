"""decode_input (1030:0DC1) — the input-source decoder.

Byte-exact vs the ASM is proven offline over the demos (pre2/probes/probe_input_decode_shadow.py: 144 calls,
0 mismatch / 0 unmodeled across 4 demos, joystick gated absent). These tests pin each path of the pure decoder
against hand-derived expectations from the disasm so it can't silently drift.
"""
from __future__ import annotations

import hashlib

import pytest

from pre2.recovered.input_decode import (decode_input, Pre2InputGap, MODE, DEMO_PTR, DEMO_BYTE,
                                         DEMO_CNT, DEMO_FLAG, DEMO_HDR_LEVEL, LEVEL)

# keyboard sources feeding each output flag (one representative each)
KEY_E8, KEY_EC, KEY_EA, KEY_ED, KEY_EB = 0x282D, 0x2841, 0x283C, 0x283F, 0x2844
JOY_DISABLE = 0x27D9   # nonzero => joystick absent (the keyboard-play case)


def _mem(init):
    data = dict(init)

    def rb(o):
        return data.get(o & 0xFFFF, 0)

    def rw(o):
        return rb(o) | (rb((o + 1) & 0xFFFF) << 8)

    return rb, rw, data


def _apply(writes, data):
    for o, (v, wid) in writes.items():
        for k in range(wid):
            data[(o + k) & 0xFFFF] = (v >> (8 * k)) & 0xFF


def test_live_no_input_records_header_and_zero_entry():
    # mode 0, cursor at 0, no keys: all six flags cleared, and the record tail stamps the level header then
    # writes a fresh {byte=0, count=0} entry and advances the cursor by 2. [asm 0E5B + 0F48]
    rb, rw, _ = _mem({MODE: 0, DEMO_PTR: 0, JOY_DISABLE: 1, LEVEL: 9})
    w = decode_input(rb, rw)
    assert all(w[o] == (0, 1) for o in (0x27E8, 0x27E9, 0x27EA, 0x27EB, 0x27EC, 0x27ED))
    assert w[DEMO_HDR_LEVEL] == (9, 1)
    assert w[0x3F] == (0, 1) and w[0x40] == (0, 1)
    assert w[DEMO_PTR] == (2, 2)


def test_live_key_sets_flag_and_packs_into_record():
    # mode 0, the [0x2841] key -> [0x27EC]=0xFF; packed byte = bit1 set = 2 (PACK order e9,e8,eb,ea,ec,ed).
    rb, rw, _ = _mem({MODE: 0, DEMO_PTR: 0, JOY_DISABLE: 1, KEY_EC: 0xFF})
    w = decode_input(rb, rw)
    assert w[0x27EC] == (0xFF, 1)
    assert w[0x27E8] == (0, 1) and w[0x27EA] == (0, 1)
    assert w[0x3F] == (2, 1)            # packed input recorded


def test_live_multiple_keys_pack_bits():
    # [0x282D]->E8 (bit4) and [0x283F]->ED (bit0): packed = 0b010001 = 0x11.
    rb, rw, _ = _mem({MODE: 0, DEMO_PTR: 0, JOY_DISABLE: 1, KEY_E8: 0xFF, KEY_ED: 0xFF})
    w = decode_input(rb, rw)
    assert w[0x27E8] == (0xFF, 1) and w[0x27ED] == (0xFF, 1)
    assert w[0x3F] == (0x11, 1)


def test_live_rle_increments_repeat_when_unchanged():
    # cursor=2, the previous entry's byte ([0x3F], read as [si+0x3D]) equals the new packed byte and its count
    # ([0x40]) has room: bump the repeat count, do NOT advance the cursor. [asm 0F5D inc]
    rb, rw, _ = _mem({MODE: 0, DEMO_PTR: 2, JOY_DISABLE: 1, KEY_EC: 0xFF, 0x3F: 2, 0x40: 5})
    w = decode_input(rb, rw)
    assert w[0x40] == (6, 1)            # [si+0x3E] = [0x40] incremented
    assert DEMO_PTR not in w            # cursor unchanged
    assert 0x41 not in w               # no new entry


def test_live_rle_new_entry_when_changed():
    # cursor=2, previous byte (2) != new packed byte (E8 pressed -> 0x10): write a new entry + advance.
    rb, rw, _ = _mem({MODE: 0, DEMO_PTR: 2, JOY_DISABLE: 1, KEY_E8: 0xFF, 0x3F: 2, 0x40: 5})
    w = decode_input(rb, rw)
    assert w[0x41] == (0x10, 1) and w[0x42] == (0, 1)
    assert w[DEMO_PTR] == (4, 2)


def test_live_rle_new_entry_when_count_maxed():
    # same input but the repeat count is already 0xFF: must start a new entry instead of overflowing.
    rb, rw, _ = _mem({MODE: 0, DEMO_PTR: 2, JOY_DISABLE: 1, KEY_EC: 0xFF, 0x3F: 2, 0x40: 0xFF})
    w = decode_input(rb, rw)
    assert w[0x41] == (2, 1) and w[0x42] == (0, 1)
    assert w[DEMO_PTR] == (4, 2)


def test_record_buffer_full_stops_recording():
    # cursor at the limit: outputs still decode but nothing is appended. [asm 0F0E jae]
    rb, rw, _ = _mem({MODE: 0, DEMO_PTR: 0x7FC, JOY_DISABLE: 1, KEY_EC: 0xFF})
    w = decode_input(rb, rw)
    assert w[0x27EC] == (0xFF, 1)
    assert DEMO_PTR not in w and 0x3F not in w


def test_playback_reuse_decrements_count():
    # mode 1, cursor!=0, count>0: reuse the current byte, decrement the count, unpack to flags, no keyboard.
    rb, rw, _ = _mem({MODE: 1, DEMO_PTR: 2, DEMO_CNT: 3, DEMO_BYTE: 0x02})   # 0x02 -> bit1 -> [0x27EC]
    w = decode_input(rb, rw)
    assert w[DEMO_CNT] == (2, 1)
    assert w[0x27EC] == (1, 1)          # playback unpacks to 0/1 (not 0xFF)
    assert w[0x27E8] == (0, 1)
    assert DEMO_BYTE not in w           # not re-read


def test_playback_reads_next_entry():
    # mode 1, count==0: read the next 2-byte entry at [ptr+0x3F], advance, latch byte+count, unpack.
    rb, rw, _ = _mem({MODE: 1, DEMO_PTR: 2, DEMO_CNT: 0, 0x41: 0x01, 0x42: 0x07})  # [si+0x3F]=0x41
    w = decode_input(rb, rw)
    assert w[DEMO_PTR] == (4, 2)
    assert w[DEMO_BYTE] == (0x01, 1) and w[DEMO_CNT] == (0x07, 1)
    assert w[0x27ED] == (1, 1)          # 0x01 -> bit0 -> [0x27ED]


def test_playback_end_sentinel_sets_flag():
    # the 0x55AA end-of-demo word sets [0x6BE5]. [asm 0DF7]
    rb, rw, _ = _mem({MODE: 1, DEMO_PTR: 2, DEMO_CNT: 0, 0x41: 0xAA, 0x42: 0x55})
    w = decode_input(rb, rw)
    assert w[DEMO_FLAG] == (1, 1)


def test_joystick_present_fails_loud():
    # mode 0 with the joystick gate open (cfg bit7 clear, disable flag 0): the port 0x201 read is unrecovered.
    rb, rw, _ = _mem({MODE: 0, DEMO_PTR: 0, 0x27E4: 0x00, JOY_DISABLE: 0})
    with pytest.raises(Pre2InputGap):
        decode_input(rb, rw)


def test_pack_unpack_roundtrip_via_recorded_demo():
    # Record a live input, then play the recorded buffer back: the six flags must come back bit-for-bit. This is
    # the property the format relies on (record re-pack == inverse of playback unpack).
    rb, rw, data = _mem({MODE: 0, DEMO_PTR: 0, JOY_DISABLE: 1,
                         KEY_E8: 0xFF, KEY_EC: 0xFF, KEY_ED: 0xFF})
    _apply(decode_input(rb, rw), data)
    recorded = data[0x3F]
    # play it back from a fresh state holding only the recorded entry
    rb2, rw2, _ = _mem({MODE: 1, DEMO_PTR: 0, DEMO_CNT: 0, 0x3F: recorded, 0x40: 0})
    w = decode_input(rb2, rw2)
    assert w[0x27E8] == (1, 1) and w[0x27EC] == (1, 1) and w[0x27ED] == (1, 1)
    assert w[0x27E9] == (0, 1) and w[0x27EA] == (0, 1) and w[0x27EB] == (0, 1)


def test_golden_sequence_hash():
    # Lock the decoder's behaviour over a deterministic multi-frame live sequence (records as it goes).
    rb, rw, data = _mem({MODE: 0, DEMO_PTR: 0, JOY_DISABLE: 1, LEVEL: 9})
    keys = [KEY_E8, KEY_EC, KEY_EC, KEY_EC, 0, KEY_ED, KEY_EA]
    log = []
    for k in keys:
        for src in (KEY_E8, KEY_EC, KEY_EA, KEY_ED, KEY_EB):
            data[src] = 0
        if k:
            data[k] = 0xFF
        w = decode_input(rb, rw)
        _apply(w, data)
        log.append(tuple(sorted((o, v, wid) for o, (v, wid) in w.items())))
    digest = hashlib.sha256(repr(log).encode()).hexdigest()[:16]
    assert digest == "2d37981948b8ae5d", digest
