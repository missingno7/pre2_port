"""Enhanced audio backend — a modern float32 mixer driven by semantic events.

Consumes the same :mod:`pre2.audio.events` stream as the faithful backend, but is
deliberately **free of every DOS/SB constraint**: it mixes in float32 at a modern
output rate (44.1 kHz default), with per-voice linear-interpolated resampling, no
8-bit wrapping arithmetic, no 168-byte DMA blocks, no IRQ/ISR timing, and no
segment:offset memory layout. The original samples/modules are used purely as
**assets** (8-bit PCM lifted to float, looped in float).

It reuses one thing from the recovered layer: the **sequencer** (the pure tracker
note/effect logic in :mod:`pre2.recovered.tracker`) for musical decisions and tempo
— that is song *content*, not mixer mechanics. The actual mixing is all modern here.
The only original timing fact it honours is the song's tick rate (tempo), which is
intrinsically ``source_rate / 168`` ≈ 50 Hz; that defines tempo, not output quality.

Pure of VM/DMA/ISR: imports only numpy + the pure assets/events/sequencer.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from pre2.audio.assets import SOURCE_RATE, Module
from pre2.audio.events import (
    GameAudioEvent, PlaySfx, SetMusicEnabled, SetSfxEnabled, SetVolume,
    StartSong, StopSong,
)
from pre2.recovered.tracker import (
    NUM_VOICES, PlaybackState, TrackerInstrument, TrackerVoice, tracker_tick,
)

__all__ = ["EnhancedBackend", "OUT_RATE"]

OUT_RATE = 44100
_TICK_BLOCK = 168          # the faithful block = one sequencer tick (defines tempo only)
_FADE = 48                 # short attack ramp (samples) to kill note-edge clicks


def _pcm_to_float(pcm: bytes) -> np.ndarray:
    """Lift original 8-bit *signed* PCM (MOD convention) to float32 [-1, 1]."""
    a = np.frombuffer(pcm, dtype=np.uint8).astype(np.int16)
    a = np.where(a >= 128, a - 256, a)
    return (a.astype(np.float32)) / 128.0


@dataclass
class _Voice:
    data: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))
    pos: float = 0.0
    advance: float = 0.0       # source-frames per output-frame
    gain: float = 0.0
    loop_start: int = 0
    loop_end: int = 0          # == sample play length (wrap point)
    looping: bool = False
    active: bool = False
    age: int = 0               # output-frames rendered (for the attack ramp)

    def render_into(self, buf: np.ndarray) -> None:
        if not self.active or self.advance <= 0.0 or self.data.shape[0] < 2:
            return
        n = buf.shape[0]
        L = self.data.shape[0]
        end = min(self.loop_end if self.loop_end > 0 else L, L)
        positions = self.pos + self.advance * np.arange(n, dtype=np.float64)
        if self.looping and end > self.loop_start + 1:
            span = end - self.loop_start
            positions = np.where(positions < end, positions,
                                 self.loop_start + np.mod(positions - end, span))
            nxt = self.pos + self.advance * n
            self.pos = (self.loop_start + (nxt - end) % span) if nxt >= end else nxt
            gate = None
        else:
            gate = positions < (end - 1)
            positions = np.clip(positions, 0.0, end - 1.0001)
            self.pos += self.advance * n
            if self.pos >= end - 1:
                self.active = False
        i0 = positions.astype(np.int64)
        np.clip(i0, 0, L - 2, out=i0)
        frac = (positions - i0).astype(np.float32)
        samp = self.data[i0] * (1.0 - frac) + self.data[i0 + 1] * frac
        if gate is not None:
            samp *= gate
        if self.age < _FADE:                      # linear attack ramp -> no edge click
            ramp = np.clip((self.age + np.arange(n)) / _FADE, 0.0, 1.0).astype(np.float32)
            samp *= ramp
        self.age += n
        buf += samp * self.gain


class EnhancedBackend:
    """Float mixer: semantic events in, high-quality float32 PCM out."""

    def __init__(self, out_rate: int = OUT_RATE) -> None:
        self.out_rate = out_rate
        self._module: Module | None = None
        self._pb: PlaybackState | None = None
        self._tvoices: list[TrackerVoice] = []
        self._tinstr: list[TrackerInstrument] = []
        self._period_table: list[int] = []
        self._music: list[_Voice] = [_Voice() for _ in range(NUM_VOICES)]
        self._sfx: list[_Voice] = []
        self._sample_cache: dict[int, np.ndarray] = {}
        self._music_on = True
        self._music_gain = 0.6        # headroom for 4 channels summed
        self._sfx_gain = 0.9
        self._frac_to_tick = 0.0

    @property
    def _samples_per_tick(self) -> float:
        # one sequencer tick per faithful block; tempo is source_rate/168 Hz.
        sr = self._module.source_rate if self._module else SOURCE_RATE
        return self.out_rate * _TICK_BLOCK / sr

    # -- event sink -----------------------------------------------------------
    def handle(self, event: GameAudioEvent) -> None:
        if isinstance(event, StartSong):
            self._start_song(event.module)
        elif isinstance(event, StopSong):
            self._module = None
            for v in self._music:
                v.active = False
        elif isinstance(event, SetMusicEnabled):
            self._music_on = event.enabled
        elif isinstance(event, SetSfxEnabled):
            pass
        elif isinstance(event, SetVolume):
            if event.music is not None:
                self._music_gain = max(0.0, event.music)
            if event.sfx is not None:
                self._sfx_gain = max(0.0, event.sfx)
        elif isinstance(event, PlaySfx):
            self._play_sfx(event)

    def _start_song(self, module: Module) -> None:
        self._module = module
        self._pb = PlaybackState(tick=module.initial_speed, speed=module.initial_speed,
                                 order_pos=0, row=0)
        self._tvoices = [TrackerVoice(pos=0xFFFF, end=0, instrument=0, period=0, volume=0,
                                      frac=0, volume_slide=0, note_period=0, effect=0)
                         for _ in range(NUM_VOICES)]
        self._tinstr = [TrackerInstrument(length=s.length, default_volume=s.default_volume)
                        for s in module.samples]
        self._period_table = list(module.period_table)
        self._music = [_Voice() for _ in range(NUM_VOICES)]
        self._sample_cache.clear()
        self._frac_to_tick = 0.0

    def _float_sample(self, instrument: int) -> np.ndarray:
        cached = self._sample_cache.get(instrument)
        if cached is None:
            cached = _pcm_to_float(self._module.samples[instrument].pcm)
            self._sample_cache[instrument] = cached
        return cached

    def _play_sfx(self, ev: PlaySfx) -> None:
        v = _Voice(data=_pcm_to_float(ev.pcm), pos=0.0,
                   advance=ev.source_rate / self.out_rate,
                   gain=(ev.volume / 64.0) * self._sfx_gain,
                   loop_end=len(ev.pcm), looping=False, active=len(ev.pcm) > 1)
        self._sfx.append(v)

    # -- sequencer tick -------------------------------------------------------
    def _do_tick(self) -> None:
        if not self._music_on or self._module is None or self._pb is None:
            return
        mod, pb = self._module, self._pb
        if pb.order_pos > mod.song_length:
            pb.order_pos = 0
        pattern = mod.patterns.get(mod.order[pb.order_pos])
        if pattern is None:
            return
        process_row = (pb.tick == 1)      # this tick reaches 0 -> a new row is read
        row = pb.row
        tracker_tick(pb, self._tvoices, pattern, bytes(mod.order), mod.song_length,
                     self._period_table, self._tinstr)
        if process_row:
            for ch in range(NUM_VOICES):
                cell = pattern[row * 16 + ch * 4: row * 16 + ch * 4 + 4]
                if len(cell) == 4 and ((cell[2] >> 4) | (cell[1] & 0x10)) != 0:
                    self._trigger(ch)
        # per-tick volume (incl. slides) tracks the sequencer's channel volume
        for ch in range(NUM_VOICES):
            self._music[ch].gain = (self._tvoices[ch].volume / 64.0) * self._music_gain

    def _trigger(self, ch: int) -> None:
        tv = self._tvoices[ch]
        if not (0 <= tv.instrument < len(self._module.samples)):
            return
        samp = self._module.samples[tv.instrument]
        self._music[ch] = _Voice(
            data=self._float_sample(tv.instrument), pos=0.0,
            advance=self._module.source_rate * tv.period / (256.0 * self.out_rate),
            gain=(tv.volume / 64.0) * self._music_gain,
            loop_start=samp.loop_start, loop_end=samp.length, looping=samp.loops,
            active=(tv.period > 0 and samp.length > 1),
        )

    # -- output ---------------------------------------------------------------
    def render(self, n_samples: int) -> np.ndarray:
        """Render ``n_samples`` of float32 mono audio, ticking the sequencer as needed."""
        out = np.zeros(n_samples, np.float32)
        i = 0
        while i < n_samples:
            if self._frac_to_tick <= 0.0:
                self._do_tick()
                self._frac_to_tick += self._samples_per_tick
            chunk = min(n_samples - i, int(self._frac_to_tick) or 1)
            seg = out[i:i + chunk]
            for v in self._music:
                v.render_into(seg)
            for v in self._sfx:
                v.render_into(seg)
            self._sfx = [v for v in self._sfx if v.active]
            self._frac_to_tick -= chunk
            i += chunk
        # soft limiter: tame summed peaks without hard-clip clicks
        np.tanh(out, out=out)
        return out
