"""Pure unit tests for the recovered audio mixer (216B + SFX composition).

Byte-exact fidelity vs the ASM is covered in-VM by pre2/probes/verify_mixer.py
(mix_channel) and verify_mixer_block.py (full block). These fast tests pin the
logic: additive volume-scaled mix, resample step, loop/end, SFX overlay, and the
channel-3-only-without-SFX composition rule.
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pre2.recovered.mixer import (  # noqa: E402
    BLOCK_LEN, CHANNEL_OFF, ChannelState, Instrument, Sfx,
    mix_block, mix_channel, mix_sfx,
)

# identity volume table at volume 0: scaled == sample byte (row = 0<<6 = 0)
_VOL_IDENTITY = bytes(i & 0xFF for i in range(65 * 64 + 256))


def _ch(pos=0, end=8, instrument=0, period=0, volume=0, frac=0):
    return ChannelState(pos, end, instrument, period, volume, frac)


def test_mix_channel_additive_1to1_at_volume_zero():
    sample = bytes([10, 20, 30, 40, 50, 60, 70, 80, 90])
    instr = Instrument(loop_start=0, loop_len=2, sample=sample)
    buf = bytearray([1, 2, 3, 4, 5, 6, 7, 8])     # mixes additively on top
    mix_channel(buf, _ch(pos=0, end=64, period=0), instr, _VOL_IDENTITY, block_len=8)
    assert list(buf) == [11, 22, 33, 44, 55, 66, 77, 88]  # base + sample (period 0 => 1:1)


def test_mix_channel_byte_wraps_on_overflow():
    instr = Instrument(0, 2, bytes([200, 100, 0, 0]))
    buf = bytearray([100, 200, 0, 0])
    mix_channel(buf, _ch(pos=0, end=64, period=0), instr, _VOL_IDENTITY, block_len=4)
    assert list(buf) == [(100 + 200) & 0xFF, (200 + 100) & 0xFF, 0, 0]  # wraps mod 256


def test_mix_channel_silent_channel_is_untouched():
    buf = bytearray([5, 5, 5, 5])
    out = mix_channel(buf, _ch(pos=CHANNEL_OFF), Instrument(0, 0, b"\x00" * 8), _VOL_IDENTITY, 4)
    assert list(buf) == [5, 5, 5, 5] and out.pos == CHANNEL_OFF


def test_mix_channel_loops_and_keeps_filling_the_block():
    # loop region [0,4); loop_len=4 (>2) loops. When si passes end mid-block the asm
    # wraps to loop_start and KEEPS filling the rest of the block (it does not stop),
    # so the block is fully written and pos lands wherever filling ended (not loop_start).
    instr = Instrument(loop_start=0, loop_len=4, sample=bytes(range(40)), ptr_off=0)
    out = mix_channel(buf := bytearray(8), _ch(pos=0, end=4, period=0), instr, _VOL_IDENTITY, 8)
    assert list(buf) == [0, 1, 2, 3, 4, 0, 1, 2]   # filled past the wrap, not early-returned
    assert out.pos != CHANNEL_OFF and out.end == 4


def test_mix_channel_fades_and_stops_when_no_loop():
    # loop_len <= 2 == "no loop": one-shot. When the sample ends mid-block the asm
    # applies a linear release fade over the rest of the block, then silences the channel.
    instr = Instrument(loop_start=0, loop_len=2, sample=bytes(range(40)), ptr_off=0)
    out = mix_channel(buf := bytearray(8), _ch(pos=0, end=4, period=0), instr, _VOL_IDENTITY, 8)
    assert list(buf) == [0, 1, 2, 3, 4, 4, 3, 2]   # base samples then decaying release tail
    assert out.pos == CHANNEL_OFF


def test_mix_sfx_copies_then_pads_and_advances():
    buf = bytearray([9] * 8)
    out = mix_sfx(buf, Sfx(pos=0x100, remaining=3, sample=bytes([1, 2, 3])), block_len=8)
    assert list(buf) == [1, 2, 3, 0, 0, 0, 0, 0]
    assert out.remaining == 0 and out.pos == 0x103


def test_mix_sfx_silence_when_no_effect():
    buf = bytearray([7] * 8)
    mix_sfx(buf, Sfx(pos=0, remaining=0, sample=b""), block_len=8)
    assert list(buf) == [0] * 8


def test_mix_block_channel3_only_without_sfx():
    # 4 channels each contributing a constant via identity table
    instrs = [Instrument(0, 2, bytes([k + 1] * 16)) for k in range(4)]
    chans = [_ch(pos=0, end=64, instrument=k) for k in range(4)]

    # no SFX -> silence base + all 4 channels
    buf = bytearray(8)
    mix_block(buf, chans, instrs, _VOL_IDENTITY, Sfx(0, 0, b""), music_on=True, block_len=8)
    assert buf[0] == 1 + 2 + 3 + 4   # all four channels

    # active SFX still playing after this block (remaining > block_len) -> channel 3 skipped
    buf = bytearray(8)
    sfx = Sfx(pos=0, remaining=16, sample=bytes([100] * 8))
    mix_block(buf, chans, instrs, _VOL_IDENTITY, sfx, music_on=True, block_len=8)
    assert buf[0] == 100 + 1 + 2 + 3   # SFX base + channels 0,1,2 (not 3)


def test_mix_block_music_off_is_sfx_only():
    instrs = [Instrument(0, 2, bytes([9] * 16)) for _ in range(4)]
    chans = [_ch(pos=0, end=64, instrument=k) for k in range(4)]
    buf = bytearray(8)
    mix_block(buf, chans, instrs, _VOL_IDENTITY, Sfx(0, 0, b""), music_on=False, block_len=8)
    assert list(buf) == [0] * 8   # music off, no sfx => silence
