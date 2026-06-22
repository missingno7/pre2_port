"""Enhanced audio backend — modern float32 mixer driven by semantic events.

Consumes the :mod:`pre2.audio.events` stream and renders **float32 / 44.1 kHz stereo**,
fully detached from the DOS/SB world:

* music: :class:`pre2.audio.mod_player.ModPlayer` plays the standard ``.TRK`` module
  carried by ``StartSong`` (standard Amiga pitch + tempo), not the PRE2 mixer's
  internal step table;
* SFX: each ``PlaySfx`` resolves to a one-shot float voice (the signed 8-bit
  ``SAMPLE.SQZ`` sample), resampled to the output rate and panned/scaled.

It imports only numpy + the pure player/asset model — **no** ``period_table``, resample
step, 8.4 kHz rate, 168-byte block, DMA/ISR or VM. That purity is the point: the
enhanced backend is the pressure test for "is the audio expressed as game intent?".
The faithful backend remains the separate byte-exact oracle.
"""
from __future__ import annotations

import numpy as np

from pre2.audio.events import (
    GameAudioEvent, PlaySfx, SetMusicEnabled, SetSfxEnabled, SetVolume,
    StartSong, StopSong,
)
from pre2.audio.mod_player import _Chan, ModPlayer
from pre2.codecs.audio import SFX_SAMPLE_RATE

__all__ = ["EnhancedBackend", "OUT_RATE"]

OUT_RATE = 44100


def _sfx_voice(ev: PlaySfx, out_rate: int) -> _Chan:
    a = np.frombuffer(ev.pcm, dtype=np.uint8).astype(np.int16)
    a = np.where(a >= 128, a - 256, a)
    data = (a.astype(np.float32)) / 128.0
    rate = ev.source_rate or SFX_SAMPLE_RATE
    return _Chan(pan=0.5, data=data, advance=rate / out_rate, volume=min(ev.volume, 64),
                 loop_end=len(data), looping=False, active=len(data) > 1)


_MAX_TICK_BUDGET = 16        # ~320 ms; absorbs jitter, caps catch-up after a long stall


class EnhancedBackend:
    """Float mixer: semantic events in, high-quality float32 stereo PCM out.

    Two clocks, deliberately separated:

    * **render(n)** advances the active voices by *audio time* (continuous PCM), driven
      by the audio device. It never gaps a held note.
    * the **sequencer** advances only when *game audio time* supplies a tick via
      :meth:`advance_ticks`. Slow game -> ticks arrive later -> notes are held longer
      (each still clean), exactly like playing a piano slowly. ``free_run=True`` ignores
      the budget and ticks at the song's own tempo (offline / standalone rendering).

    SFX voices are pure audio-time one-shots (not sequencer-gated)."""

    def __init__(self, out_rate: int = OUT_RATE, *, free_run: bool = False) -> None:
        self.out_rate = out_rate
        self._player: ModPlayer | None = None
        self._sfx: list[_Chan] = []
        self._music_on = True
        self._music_gain = 0.7        # matches the standalone ModPlayer level
        self._sfx_gain = 0.9
        self._free_run = free_run
        self._tick_budget = 0
        self._samples_to_tick = 0.0

    # -- event sink -----------------------------------------------------------
    def handle(self, event: GameAudioEvent) -> None:
        if isinstance(event, StartSong):
            self._player = ModPlayer(event.module, out_rate=self.out_rate, loop=event.loop)
            self._samples_to_tick = 0.0
            self._tick_budget = 0
        elif isinstance(event, StopSong):
            self._player = None
        elif isinstance(event, SetMusicEnabled):
            self._music_on = event.enabled
        elif isinstance(event, SetSfxEnabled):
            if not event.enabled:
                self._sfx.clear()
        elif isinstance(event, SetVolume):
            if event.music is not None:
                self._music_gain = max(0.0, event.music)
            if event.sfx is not None:
                self._sfx_gain = max(0.0, event.sfx)
        elif isinstance(event, PlaySfx):
            self._sfx.append(_sfx_voice(event, self.out_rate))

    def advance_ticks(self, k: int) -> None:
        """Supply ``k`` ticks of game audio time (1 SB block == 1 tracker tick)."""
        if not self._free_run and k > 0:
            self._tick_budget = min(self._tick_budget + k, _MAX_TICK_BUDGET)

    # -- output ---------------------------------------------------------------
    def render(self, n_frames: int) -> np.ndarray:
        """Render ``n_frames`` of float32 stereo, shape ``(n, 2)`` -- driven by audio time.

        Voices advance continuously; the sequencer ticks at ``samples_per_tick`` boundaries
        only while game-time ticks are available (else the current notes sustain)."""
        out = np.zeros((n_frames, 2), np.float32)
        p = self._player
        if p is not None and self._music_on:
            i = 0
            while i < n_frames:
                if self._samples_to_tick <= 0.0:
                    if self._free_run or self._tick_budget > 0:
                        p.tick()
                        if not self._free_run:
                            self._tick_budget -= 1
                    self._samples_to_tick += p.samples_per_tick
                chunk = min(n_frames - i, max(1, int(self._samples_to_tick)))
                out[i:i + chunk] += p.render_voices(chunk)
                self._samples_to_tick -= chunk
                i += chunk
            out *= self._music_gain
        if self._sfx:
            sl = np.zeros(n_frames, np.float32)
            sr = np.zeros(n_frames, np.float32)
            for v in self._sfx:
                v.render_into(sl, sr)
            out[:, 0] += sl * self._sfx_gain
            out[:, 1] += sr * self._sfx_gain
            self._sfx = [v for v in self._sfx if v.active]
        np.tanh(out, out=out)         # soft limiter (music + SFX), no hard-clip clicks
        return out
