"""Pure unit tests for the recovered music tracker / sequencer (221A).

Byte-exact fidelity vs the ASM is covered in-VM by pre2/probes/verify_tracker.py
(lockstep at 1030:221A). These fast tests pin the logic: per-tick volume slides,
the speed-gated row processing, note triggering, the period->step lookup, and the
five MOD effects PRE2 actually uses (A/B/C/D/F).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pre2.recovered.tracker import (  # noqa: E402
    NUM_VOICES, PlaybackState, TrackerInstrument, TrackerVoice, tracker_tick,
)

# period table: identity for the small periods used here
_PERIODS = list(range(0x8000))
_INSTRS = [
    TrackerInstrument(length=0, default_volume=0),
    TrackerInstrument(length=0x1000, default_volume=0x30),   # sample #1 (b1 nibble logic)
    TrackerInstrument(length=0x2000, default_volume=0x20),   # sample #2
]
_EMPTY_PATTERN = bytes(0x400)


def _voice(**kw):
    base = dict(pos=0, end=0, instrument=0, period=0, volume=0, frac=0,
                volume_slide=0, note_period=0, effect=0)
    base.update(kw)
    return TrackerVoice(**base)


def _voices():
    return [_voice() for _ in range(NUM_VOICES)]


def _cell(period=0, sample=0, effect=0, param=0):
    # PRE2 in-memory cell: b0|b1<<8 = period (15 bits); sample = (b2>>4)|(b1&0x10)
    b0 = period & 0xFF
    b1 = ((period >> 8) & 0x7F) | (0x10 if sample & 0x10 else 0)
    b2 = ((sample & 0x0F) << 4) | (effect & 0x0F)
    b3 = param & 0xFF
    return bytes([b0, b1, b2, b3])


def _pattern(row0):
    p = bytearray(0x400)
    p[0:len(row0)] = row0
    return bytes(p)


def test_tick_countdown_only_processes_row_on_zero():
    pb = PlaybackState(tick=3, speed=6, order_pos=0, row=5)
    voices = _voices()
    tracker_tick(pb, voices, _EMPTY_PATTERN, b"\x00", 1, _PERIODS, _INSTRS)
    assert pb.tick == 2 and pb.row == 5   # not yet at a row boundary


def test_row_processed_and_advances_when_tick_hits_zero():
    pb = PlaybackState(tick=1, speed=6, order_pos=0, row=5)
    voices = _voices()
    tracker_tick(pb, voices, _EMPTY_PATTERN, b"\x00", 1, _PERIODS, _INSTRS)
    assert pb.tick == 6      # reloaded speed
    assert pb.row == 6       # advanced one row


def test_note_trigger_sets_voice_from_instrument():
    pb = PlaybackState(tick=1, speed=6, order_pos=0, row=0)
    voices = _voices()
    cell = _cell(period=0x100, sample=2)   # sample #2 -> instruments[1]
    tracker_tick(pb, voices, _pattern(cell), b"\x00", 1, _PERIODS, _INSTRS)
    v = voices[0]
    assert v.instrument == 1 and v.pos == 0 and v.end == 0x1000
    assert v.volume == 0x30 and v.frac == 0
    assert v.note_period == 0x100 and v.period == _PERIODS[0x100]


def test_effect_c_sets_volume_clamped():
    pb = PlaybackState(tick=1, speed=6, order_pos=0, row=0)
    voices = _voices()
    tracker_tick(pb, voices, _pattern(_cell(effect=0xC, param=0x50)), b"\x00", 1, _PERIODS, _INSTRS)
    assert voices[0].volume == 0x40   # 0x50 clamped to VOL_MAX


def test_effect_f_sets_speed():
    pb = PlaybackState(tick=1, speed=6, order_pos=0, row=0)
    voices = _voices()
    tracker_tick(pb, voices, _pattern(_cell(effect=0xF, param=4)), b"\x00", 1, _PERIODS, _INSTRS)
    assert pb.speed == 4


def test_effect_a_volume_slide_applies_next_tick():
    # Axy: x>0 => slide up by x; row sets the slide, the following tick applies it.
    pb = PlaybackState(tick=1, speed=6, order_pos=0, row=0)
    voices = _voices()
    voices[0].volume = 0x10
    tracker_tick(pb, voices, _pattern(_cell(effect=0xA, param=0x30)), b"\x00", 1, _PERIODS, _INSTRS)
    assert voices[0].volume_slide == 3
    tracker_tick(pb, voices, _EMPTY_PATTERN, b"\x00", 1, _PERIODS, _INSTRS)  # next tick
    assert voices[0].volume == 0x13   # +3


def test_effect_a_slide_down_clamps_at_zero():
    pb = PlaybackState(tick=1, speed=6, order_pos=0, row=0)
    voices = _voices()
    voices[0].volume = 1
    tracker_tick(pb, voices, _pattern(_cell(effect=0xA, param=0x05)), b"\x00", 1, _PERIODS, _INSTRS)
    assert voices[0].volume_slide & 0x8000   # negative (down by 5)
    tracker_tick(pb, voices, _EMPTY_PATTERN, b"\x00", 1, _PERIODS, _INSTRS)
    assert voices[0].volume == 0   # clamped, not negative


def test_effect_d_pattern_break_advances_order():
    pb = PlaybackState(tick=1, speed=6, order_pos=0, row=0)   # cell sits at row 0
    voices = _voices()
    tracker_tick(pb, voices, _pattern(_cell(effect=0xD, param=5)), b"\x00\x01", 2, _PERIODS, _INSTRS)
    assert pb.order_pos == 1 and pb.row == 5   # break to row (param), next order


def test_pos_jump_on_early_channel_does_not_corrupt_later_channels():
    # Regression: a Bxx/Dxx effect on channel 0 sets pb.row for the *advance*, but the
    # remaining channels of THIS row must still be read from the latched row pointer.
    # (Previously the per-channel offset used the mutated pb.row -> pattern[0xFFFF*16:]
    # -> IndexError inside the audio ISR -> level-end music freeze.)
    pb = PlaybackState(tick=1, speed=6, order_pos=0, row=3)
    voices = _voices()
    row = bytearray(16)
    row[0:4] = _cell(effect=0xB, param=0)          # ch0: position jump (sets pb.row)
    row[4:8] = _cell(period=0x100, sample=2)       # ch1: a real note in the SAME row
    pattern = bytearray(0x400)
    pattern[3 * 16:3 * 16 + 16] = row              # place the row at pb.row=3
    tracker_tick(pb, voices, bytes(pattern), b"\x00\x01", 1, _PERIODS, _INSTRS)
    assert voices[1].instrument == 1 and voices[1].end == 0x1000   # ch1 note still triggered
    assert pb.order_pos == 0 and pb.row == 0                       # jump took effect for next tick


def test_order_wraps_at_song_end():
    # SONG_LENGTH [0xDC0] is the *last* order index, so wrap fires when next > it.
    pb = PlaybackState(tick=1, speed=1, order_pos=1, row=0x3F)  # at last order, last row
    voices = _voices()
    tracker_tick(pb, voices, _EMPTY_PATTERN, b"\x00\x01", 1, _PERIODS, _INSTRS)
    assert pb.order_pos == 0 and pb.row == 0   # next (2) > 1 => wrap to start
