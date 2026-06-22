"""``RecoveredAudioSystem`` — the single owner of recovered audio time + state.

This is the root both audio outputs branch from:

    VM / oracle → recovered commands + audio model → **RecoveredAudioSystem**
                                                       ├─ faithful render  (byte-exact)
                                                       └─ enhanced render  (modern, free)

It owns the recovered model (one song's order/patterns/instruments + the live per-voice
tracker/mixer state + SFX) and the **single musical clock** (the recovered sequencer
``tracker_tick``). Commands (:meth:`start_song` / :meth:`play_sfx` / :meth:`stop_song` /
:meth:`set_music_enabled`) come in via the recovered command layer; renderers read the
state out. The two render strategies share this one clock and model so they never drift
into a parallel sequencer:

* **faithful** — :meth:`render_faithful_block` advances the clock and mixes one 8-bit
  ``BLOCK_LEN`` block through the recovered mixer (the verified
  :class:`pre2.recovered.audio_system.AudioSystem`): the trusted reconstruction, byte-exact
  to the original ISR (``pre2/probes/verify_audio_system.py``);
* **enhanced** — a modern renderer drives :meth:`advance_tick` (the sequencer alone) and
  reads the recovered per-voice note/pitch/volume intent, rendering it however it likes
  (float, 44.1 kHz, better resampling) with no SB/DMA/IRQ/block-size constraint.

A given instance is driven by **one** strategy at a time (faithful *or* enhanced): the
faithful mixer and the enhanced clock advance the per-voice sample read differently, so
:meth:`render_faithful_block` and :meth:`advance_tick` must not be interleaved on one
instance. Both, however, run the *same* ``tracker_tick`` — that is the shared root.

Pure: only the recovered logic + the neutral asset/event model. No cpu/mem/dos_re/SB.
"""
from __future__ import annotations

from pre2.audio.assets import SOURCE_RATE, Module
from pre2.audio.events import (
    GameAudioEvent, PlaySfx, SetMusicEnabled, SetSfxEnabled, StartSong, StopSong,
)
from pre2.recovered.audio_system import AudioState, AudioSystem
from pre2.recovered.mixer import BLOCK_LEN, CHANNEL_OFF, Instrument, Sfx
from pre2.recovered.tracker import (
    NUM_VOICES, PlaybackState, TrackerInstrument, TrackerVoice,
)

__all__ = ["RecoveredAudioSystem", "audio_state_from_module", "BLOCK_LEN", "SOURCE_RATE"]

# Enhanced-clock bookkeeping: after a sequencer tick the system marks each just-triggered
# voice (the tracker set pos=0) with this "sustaining" sentinel, so a *repeated* same-note
# trigger on the next tick (tracker sets pos=0 again) is still detected. Only the enhanced
# clock writes it, and the enhanced renderer reads pos only as the retrigger flag — never as
# a real sample offset (it owns its own float position).
_SUSTAIN = 0xFFFE


def audio_state_from_module(module: Module, *, music_on: bool = True) -> AudioState:
    """Build a fresh :class:`AudioState` (song at the top) from a neutral module.

    The single construction path for the recovered model — the faithful backend and the
    enhanced renderer both start from this same recovered state."""
    return AudioState(
        pb=PlaybackState(tick=module.initial_speed, speed=module.initial_speed,
                         order_pos=0, row=0),
        voices=[TrackerVoice(pos=CHANNEL_OFF, end=0, instrument=0, period=0, volume=0,
                             frac=0, volume_slide=0, note_period=0, effect=0)
                for _ in range(NUM_VOICES)],
        order_table=bytes(module.order),
        patterns=dict(module.patterns),
        song_length=module.song_length,
        period_table=list(module.period_table),
        tracker_instruments=[TrackerInstrument(length=s.length, default_volume=s.default_volume)
                             for s in module.samples],
        mixer_instruments=[Instrument(loop_start=s.loop_start, loop_len=s.loop_len,
                                      sample=s.pcm, ptr_off=0) for s in module.samples],
        vol_table=module.vol_table,
        sfx=Sfx(pos=0, remaining=0, sample=b""),
        music_on=music_on,
    )


class RecoveredAudioSystem:
    """Owns the recovered audio model + clock; faithful & enhanced are strategies over it."""

    def __init__(self) -> None:
        self._sys: AudioSystem | None = None
        self._module: Module | None = None
        self._music_on = True
        self.tick_count = 0
        self.song_id = 0                          # bumped each start_song (lets a renderer notice)
        self._sfx_events: list[PlaySfx] = []     # enhanced SFX queue (drained by the renderer)

    # -- recovered command layer ---------------------------------------------------
    def start_song(self, module: Module) -> None:
        """StartSong: install a recovered :class:`Module` and reset to the song top."""
        self._module = module
        self._sys = AudioSystem(audio_state_from_module(module, music_on=self._music_on))
        self.tick_count = 0
        self.song_id += 1

    def stop_song(self) -> None:
        self._module = None
        self._sys = None

    def set_music_enabled(self, on: bool) -> None:
        self._music_on = bool(on)
        if self._sys is not None:
            self._sys.s.music_on = self._music_on

    def play_sfx(self, pcm: bytes, *, volume: int = 0x40, source_rate: int = SOURCE_RATE) -> None:
        """PlaySfx: one-shot sample. Feeds BOTH strategies from the one command —
        the faithful overlay (channel-3-borrowed 8-bit Sfx) and the enhanced queue."""
        if not pcm:
            return
        if self._sys is not None:
            self._sys.s.sfx = Sfx(pos=0, remaining=len(pcm), sample=pcm)
        self._sfx_events.append(PlaySfx(sfx_id=0, pcm=pcm, volume=volume, source_rate=source_rate))

    def handle(self, event: GameAudioEvent, *, module: Module | None = None) -> None:
        """Consume a semantic :mod:`pre2.audio.events` event (the recovered command stream).

        ``StartSong`` needs the recovered :class:`Module` (the in-memory PRE2 song the
        faithful + enhanced paths share); pass it as ``module`` (the live bridge captures it
        from VM memory). Without it, a ``StartSong`` is ignored (no parallel .TRK player)."""
        if isinstance(event, StartSong):
            if module is not None:
                self.start_song(module)
        elif isinstance(event, StopSong):
            self.stop_song()
        elif isinstance(event, SetMusicEnabled):
            self.set_music_enabled(event.enabled)
        elif isinstance(event, SetSfxEnabled):
            if not event.enabled:
                self._sfx_events.clear()
                if self._sys is not None:
                    self._sys.s.sfx = Sfx(pos=0, remaining=0, sample=b"")
        elif isinstance(event, PlaySfx):
            self.play_sfx(event.pcm, volume=event.volume, source_rate=event.source_rate)

    # -- shared model access (for the renderers) -----------------------------------
    @property
    def playing(self) -> bool:
        return self._sys is not None

    @property
    def music_on(self) -> bool:
        return self._music_on

    @property
    def module(self) -> Module | None:
        return self._module

    @property
    def voices(self) -> list:
        """The live recovered per-voice state (note/instrument/pitch/volume)."""
        return self._sys.s.voices if self._sys is not None else []

    @property
    def state(self) -> AudioState | None:
        return self._sys.s if self._sys is not None else None

    def mixer_instrument(self, idx: int) -> Instrument | None:
        """The recovered :class:`Instrument` (sample bytes + loop) for a voice's sample."""
        if self._sys is None:
            return None
        instrs = self._sys.s.mixer_instruments
        return instrs[idx] if 0 <= idx < len(instrs) else None

    def drain_sfx(self) -> list[PlaySfx]:
        """Take the SFX triggered since the last call (the enhanced renderer's one-shots)."""
        out = self._sfx_events
        self._sfx_events = []
        return out

    # -- faithful render strategy (byte-exact oracle) ------------------------------
    def render_faithful_block(self) -> bytearray:
        """One faithful 8-bit ``BLOCK_LEN`` block: advance the clock + mix through the
        recovered mixer. Byte-exact to the original ISR (the trusted reconstruction)."""
        if self._sys is None:
            return bytearray(BLOCK_LEN)
        out = self._sys.next_block()
        self.tick_count += 1
        return out

    # -- enhanced render clock (sequencer only) ------------------------------------
    def advance_tick(self) -> list[int]:
        """Advance the shared sequencer one tick for a non-faithful renderer.

        Returns the indices of voices that (re)triggered a note this tick. The enhanced
        renderer uses this to restart its float voice while reading the live
        period/volume each tick (so slides/effects carry through). No 8-bit mix runs."""
        if self._sys is None:
            return []
        voices = self._sys.s.voices
        self._sys.tick()
        self.tick_count += 1
        triggered = [i for i, v in enumerate(voices) if v.pos == 0]
        for v in voices:
            if v.pos == 0:
                v.pos = _SUSTAIN
        return triggered
