"""Prehistorik 2 native audio system — the consolidated, VM-independent audio engine.

This is the audio counterpart of ``render_frame``: Layer 5 of the audio recovery plan.
It composes the recovered tracker (:mod:`pre2.recovered.tracker`) and mixer
(:mod:`pre2.recovered.mixer`) into one self-contained engine that produces the game's
8-bit PCM **without** the ASM audio driver or the emulated Sound Blaster.

Per the original audio ISR (``1030:204D`` → DMA flip → mix), one **block** of
``BLOCK_LEN`` (168) bytes is produced per audio interrupt at the SB sample rate
(``168 / sample_rate`` ≈ 50 Hz). For each block:

  1. if music is on, ``tracker_tick`` advances the song one tick (every ``speed`` ticks a
     new pattern row triggers notes), updating the per-voice playback state;
  2. ``mix_block`` lays the SFX base then **adds** the resampled, volume-scaled MOD
     channels (channel 3 only when no SFX is active), advancing each voice's sample
     position.

``AudioState`` is the stable, plain-data input contract (module + samples + playback +
SFX), reconstructed from VM memory by ``pre2.bridge.audio_system`` (read-only). A future
high-quality backend consumes :meth:`AudioSystem.next_block` (or :meth:`render`) and
resamples the 8-bit/8.4 kHz stream cleanly to the device rate — no SB DMA, no underruns.

Pure: no ``cpu``/``mem``/``dos_re`` imports.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pre2.recovered.mixer import (
    BLOCK_LEN, ChannelState, Instrument, Sfx, mix_block,
)
from pre2.recovered.tracker import (
    PlaybackState, TrackerInstrument, TrackerVoice, tracker_tick,
)

__all__ = ["AudioState", "AudioSystem"]


@dataclass
class AudioState:
    """The complete, VM-independent input the audio engine needs (one song + its SFX)."""
    pb: PlaybackState                       # sequencer position (tick/speed/order/row)
    voices: list                            # 4x TrackerVoice (the live per-channel state)
    order_table: bytes                      # song order -> pattern index
    patterns: dict                          # pattern index -> 1024-byte pattern data
    song_length: int                        # last order index
    period_table: list                      # note period -> resample step
    tracker_instruments: list               # TrackerInstrument (length/default_volume), by sample
    mixer_instruments: list                 # Instrument (sample bytes/loop), by sample
    vol_table: bytes                        # volume scaling table
    sfx: Sfx                                # active SFX overlay (remaining==0 -> none)
    music_on: bool = True


class AudioSystem:
    """Drives :class:`AudioState` one PCM block at a time (the recovered audio ISR)."""

    def __init__(self, state: AudioState):
        self.s = state

    def next_block(self) -> bytearray:
        """Produce the next ``BLOCK_LEN``-byte 8-bit PCM block, advancing all state."""
        s = self.s
        if s.music_on:
            pattern = s.patterns[s.order_table[s.pb.order_pos]]
            tracker_tick(s.pb, s.voices, pattern, s.order_table, s.song_length,
                         s.period_table, s.tracker_instruments)
        # Project the tracker voices onto the mixer's channel view + pick each channel's
        # current instrument (the tracker may have just retriggered it).
        channels = [ChannelState(pos=v.pos, end=v.end, instrument=v.instrument,
                                 period=v.period, volume=v.volume, frac=v.frac)
                    for v in s.voices]
        instrs = [s.mixer_instruments[v.instrument] for v in s.voices]
        buf = bytearray(BLOCK_LEN)
        new_channels, s.sfx = mix_block(buf, channels, instrs, s.vol_table, s.sfx, s.music_on)
        # Write the mixer's advance (pos/end/frac) back onto the voices.
        for v, nc in zip(s.voices, new_channels):
            v.pos, v.end, v.frac = nc.pos & 0xFFFF, nc.end & 0xFFFF, nc.frac & 0xFF
        return buf

    def render(self, n_blocks: int) -> bytearray:
        """Render ``n_blocks`` blocks into one contiguous 8-bit PCM buffer."""
        out = bytearray()
        for _ in range(n_blocks):
            out += self.next_block()
        return out
