"""Composition tests for the native audio engine ``AudioSystem`` (audio Layer 5).

Guards the orchestration: per block it ticks the tracker (only when music is on) *then*
mixes, and plumbs the voices' advanced sample state back. Byte-exact fidelity vs the
original ISR is the source-of-truth check ``pre2/probes/verify_audio_system.py`` (in-VM
lockstep, 40 blocks / 0 divergence).
"""
from __future__ import annotations

import pre2.recovered.audio_system as A
from pre2.recovered.audio_system import AudioState, AudioSystem
from pre2.recovered.mixer import ChannelState, Sfx
from pre2.recovered.tracker import PlaybackState, TrackerVoice


def _state(music_on=True):
    voices = [TrackerVoice(pos=i, end=100, instrument=0, period=256, volume=64,
                           frac=0, volume_slide=0, note_period=0, effect=0) for i in range(4)]
    return AudioState(
        pb=PlaybackState(tick=1, speed=6, order_pos=0, row=0), voices=voices,
        order_table=bytes([0]), patterns={0: bytes(1024)}, song_length=0,
        period_table=[0] * 0x1000, tracker_instruments=[], mixer_instruments=[None] * 4,
        vol_table=b"", sfx=Sfx(pos=0, remaining=0, sample=b""), music_on=music_on)


def _shim(calls):
    saved = (A.tracker_tick, A.mix_block)
    A.tracker_tick = lambda *a, **k: calls.append("tick")

    def fake_mix(buf, channels, instrs, vol, sfx, music):
        calls.append("mix")
        # return advanced channels (pos+1) so we can check write-back
        return [ChannelState(pos=c.pos + 1, end=c.end, instrument=c.instrument,
                             period=c.period, volume=c.volume, frac=c.frac + 2)
                for c in channels], sfx

    A.mix_block = fake_mix
    return lambda: (setattr(A, "tracker_tick", saved[0]), setattr(A, "mix_block", saved[1]))


def test_next_block_ticks_then_mixes():
    calls = []
    restore = _shim(calls)
    try:
        sysm = AudioSystem(_state(music_on=True))
        sysm.next_block()
    finally:
        restore()
    assert calls == ["tick", "mix"]
    # the mixer's advance (pos/frac) was written back onto the voices
    assert [v.pos for v in sysm.s.voices] == [1, 2, 3, 4]
    assert all(v.frac == 2 for v in sysm.s.voices)


def test_music_off_skips_tracker():
    calls = []
    restore = _shim(calls)
    try:
        AudioSystem(_state(music_on=False)).next_block()
    finally:
        restore()
    assert calls == ["mix"]  # no tracker tick when music is off


def test_render_concatenates_blocks():
    calls = []
    restore = _shim(calls)
    try:
        out = AudioSystem(_state()).render(3)
    finally:
        restore()
    assert len(out) == 3 * A.BLOCK_LEN
    assert calls.count("tick") == 3 and calls.count("mix") == 3
