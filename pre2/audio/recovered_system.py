"""Native recovered AudioSystem — the game's audio logic, with the sound card removed.

This is the integration layer of the audio recovery. It holds the recovered model
state (song/order/patterns/instruments + the per-channel tracker/mixer state) and runs
the **recovered** ``tracker_tick`` (1030:227C) and ``mix_block`` (1030:218F/20AB) on its
OWN tick clock — **no** Sound Blaster, DMA, IRQ, 168-byte-block DMA cadence, or original
PCM transport. The SB pipeline is now only the verification oracle.

The recovered tracker's :class:`~pre2.recovered.tracker.TrackerVoice` *is* the mixer's
per-channel state (same pos/end/instrument/period/volume/frac), so the two recovered
functions form a self-contained pair: each tick the tracker triggers notes / advances
the song, each block the mixer advances the voices and produces audio.

Driven by the recovered command layer (:meth:`start_song` / :meth:`play_sfx` /
:meth:`stop_song` / :meth:`set_music_enabled`). Both the *faithful* renderer (this
module's byte-exact 8-bit block, for oracle comparison) and the *enhanced* renderer
(a modern float renderer) attach to this recovered state — below the "there is a sound
card" layer, never to the SB.

Pure: only numpy-free recovered logic + the asset model. No cpu/mem/dos_re/SB imports.
"""
from __future__ import annotations

from pre2.audio.assets import Module
from pre2.recovered.mixer import (
    BLOCK_LEN, CHANNEL_OFF, Instrument, Sfx, mix_block,
)
from pre2.recovered.tracker import (
    NUM_VOICES, PlaybackState, TrackerInstrument, TrackerVoice, tracker_tick,
)

__all__ = ["RecoveredAudioSystem", "BLOCK_LEN"]

# The PRE2 driver runs exactly one tracker tick + one mixer block per SB DMA block, a
# fixed ~50 Hz cadence (the 168-byte block at the ~8403 Hz mixer rate). The native
# system reproduces that cadence WITHOUT the SB — one tick + one block per call.
TICK_HZ = 8403 / BLOCK_LEN          # ~50.02 Hz
_SILENT = Instrument(loop_start=0, loop_len=0, sample=b"", ptr_off=0)


def _off_voice() -> TrackerVoice:
    return TrackerVoice(pos=CHANNEL_OFF, end=0, instrument=0, period=0, volume=0,
                        frac=0, volume_slide=0, note_period=0, effect=0)


class RecoveredAudioSystem:
    """The recovered audio engine. Commands in, recovered model state out.

    Output is taken from the recovered state by a renderer: :meth:`render_block` for the
    faithful 8-bit block, or (enhanced) by reading :attr:`voices` after :meth:`tick`."""

    def __init__(self) -> None:
        self.module: Module | None = None
        self.pb = PlaybackState(tick=1, speed=6, order_pos=0, row=0)
        self.voices = [_off_voice() for _ in range(NUM_VOICES)]
        self._tracker_instr: list[TrackerInstrument] = []
        self._mixer_instr: list[Instrument] = []
        self._period_table: tuple[int, ...] = ()
        self._vol_table: bytes = b""
        self.sfx = Sfx(pos=0, remaining=0, sample=b"")
        self.music_on = True

    # -- recovered command layer ---------------------------------------------------
    def start_song(self, module: Module) -> None:
        """StartSong: load a recovered :class:`Module` and reset the playback state to
        the song start (matching the original song loader's initial state)."""
        self.module = module
        self.pb = PlaybackState(tick=module.initial_speed, speed=module.initial_speed,
                                order_pos=0, row=0)
        self.voices = [_off_voice() for _ in range(NUM_VOICES)]
        self._tracker_instr = [TrackerInstrument(length=s.length, default_volume=s.default_volume)
                               for s in module.samples]
        self._mixer_instr = [Instrument(loop_start=s.loop_start, loop_len=s.loop_len,
                                        sample=s.pcm, ptr_off=0) for s in module.samples]
        self._period_table = module.period_table
        self._vol_table = module.vol_table

    def stop_song(self) -> None:
        self.module = None

    def set_music_enabled(self, on: bool) -> None:
        self.music_on = bool(on)

    def play_sfx(self, pcm: bytes) -> None:
        """PlaySfx: a one-shot 8-bit sample mixed over the music (the mixer borrows
        channel 3 while it plays, matching mix_block)."""
        self.sfx = Sfx(pos=0, remaining=len(pcm), sample=pcm)

    # -- native clock --------------------------------------------------------------
    def _tick(self) -> None:
        """One sequencer tick (recovered tracker), driven by the native clock."""
        m = self.module
        if m is None or not self.music_on:
            return
        pat = m.patterns.get(m.order[self.pb.order_pos] if self.pb.order_pos < len(m.order) else -1)
        if pat is None:
            return
        tracker_tick(self.pb, self.voices, pat, m.order, m.song_length,
                     self._period_table, self._tracker_instr)

    # -- faithful renderer (oracle-comparable) -------------------------------------
    def render_block(self) -> bytes:
        """Advance one block: tick the recovered tracker, then run the recovered mixer
        -> one BLOCK_LEN (168) byte 8-bit block, byte-exact to the original mixer."""
        self._tick()
        buf = bytearray(BLOCK_LEN)
        instr = [self._mixer_instr[v.instrument] if 0 <= v.instrument < len(self._mixer_instr)
                 else _SILENT for v in self.voices]
        new_voices, self.sfx = mix_block(buf, self.voices, instr, self._vol_table,
                                         self.sfx, self.music_on)
        self.voices = list(new_voices)
        return bytes(buf)
