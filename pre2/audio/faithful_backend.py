"""Faithful audio backend — the byte-exact oracle, driven by semantic events.

Consumes the :mod:`pre2.audio.events` stream and reproduces the *original* output
through the recovered tracker + mixer (:class:`pre2.recovered.audio_system.AudioSystem`):
8-bit unsigned PCM in 168-byte blocks at the SB rate. This is the archaeological /
verification backend — it deliberately keeps every original constraint (block size,
8-bit wrapping add, channel-3-borrowed-by-SFX, music flag), so its blocks stay
identical to the ISR oracle (proven by ``pre2/probes/verify_audio_system.py``).

It depends on the recovered faithful internals on purpose; the enhanced backend does
not. Both consume the same event objects.
"""
from __future__ import annotations

from pre2.audio.assets import Module
from pre2.audio.events import (
    GameAudioEvent, PlaySfx, SetMusicEnabled, StartSong, StopSong,
)
from pre2.recovered.audio_system import AudioState, AudioSystem
from pre2.recovered.mixer import BLOCK_LEN, CHANNEL_OFF, Instrument, Sfx
from pre2.recovered.tracker import PlaybackState, TrackerInstrument, TrackerVoice

__all__ = ["FaithfulBackend", "audio_state_from_module"]


def audio_state_from_module(module: Module, *, music_on: bool = True) -> AudioState:
    """Build a fresh :class:`AudioState` (song at the top) from a neutral module."""
    return AudioState(
        pb=PlaybackState(tick=module.initial_speed, speed=module.initial_speed,
                         order_pos=0, row=0),
        voices=[TrackerVoice(pos=CHANNEL_OFF, end=0, instrument=0, period=0, volume=0,
                             frac=0, volume_slide=0, note_period=0, effect=0)
                for _ in range(4)],
        order_table=bytes(module.order),
        patterns=dict(module.patterns),
        song_length=module.song_length,
        period_table=list(module.period_table),
        tracker_instruments=[TrackerInstrument(length=s.length, default_volume=s.default_volume)
                             for s in module.samples],
        mixer_instruments=[Instrument(loop_start=s.loop_start, loop_len=s.loop_len,
                                      sample=s.pcm, ptr_off=0)
                           for s in module.samples],
        vol_table=module.vol_table,
        sfx=Sfx(pos=0, remaining=0, sample=b""),
        music_on=music_on,
    )


class FaithfulBackend:
    """Plays the semantic event stream as the original 8-bit/block audio."""

    def __init__(self) -> None:
        self._sys: AudioSystem | None = None
        self._music_on = True

    # -- event sink -----------------------------------------------------------
    def handle(self, event: GameAudioEvent) -> None:
        if isinstance(event, StartSong):
            self._sys = AudioSystem(audio_state_from_module(event.module,
                                                            music_on=self._music_on))
        elif isinstance(event, StopSong):
            self._sys = None
        elif isinstance(event, SetMusicEnabled):
            self._music_on = event.enabled
            if self._sys is not None:
                self._sys.s.music_on = event.enabled
        elif isinstance(event, PlaySfx):
            if self._sys is not None:
                # the SFX overlay borrows the block base + channel 3 (mix_sfx/mix_block)
                self._sys.s.sfx = Sfx(pos=0, remaining=len(event.pcm), sample=event.pcm)

    # -- output ---------------------------------------------------------------
    def next_block(self) -> bytearray:
        """One 168-byte 8-bit PCM block (silence when nothing is playing)."""
        if self._sys is None:
            return bytearray(BLOCK_LEN)
        return self._sys.next_block()

    def render(self, n_blocks: int) -> bytearray:
        out = bytearray()
        for _ in range(n_blocks):
            out += self.next_block()
        return out
