"""Enhanced audio renderer — the modern interpretation of the recovered audio system.

This is *not* a faithful reconstruction of the DOS mixer and *not* an unrelated clean-room
player. It branches from the **recovered** model owned by
:class:`pre2.audio.recovered_system.RecoveredAudioSystem`: the same recovered command layer,
the same recovered song/instruments, and — crucially — the same recovered sequencer
(``tracker_tick``) driving the per-voice note/pitch/volume intent. Once it has that intent
it is free: it renders float32 / 44.1 kHz with linear-interpolated resampling and a soft
limiter, with **no** Sound Blaster, DMA, IRQ, 8-bit wrap, period-step quantisation, or
168-byte block constraint.

How it stays rooted (not clean-room): every musical decision still comes from the recovered
system — which note, on which channel, at what pitch (the recovered ``period`` resample
step), at what volume (the recovered per-voice ``volume``, slides included), and *when* (the
recovered tick clock). The renderer only changes *how that intent is turned into samples*.

Two clocks, deliberately separated (a slow game holds notes longer, never gaps them):

* **render(n)** advances active float voices by audio time (continuous PCM, device-driven);
* the **sequencer** advances via :meth:`RecoveredAudioSystem.advance_tick` only when game
  audio time supplies a tick (``advance_ticks``), or freely in ``free_run`` (offline render).
"""
from __future__ import annotations

import numpy as np

from pre2.audio.assets import SOURCE_RATE
from pre2.audio.events import PlaySfx
from pre2.audio.recovered_system import RecoveredAudioSystem
from pre2.codecs.audio import SFX_SAMPLE_RATE
from pre2.recovered.tracker import NUM_VOICES, VOL_MAX

__all__ = ["EnhancedRenderer", "OUT_RATE"]

OUT_RATE = 44100
# The recovered sequencer ticks once per faithful 168-byte block at SOURCE_RATE: ~50.02 Hz.
TICK_HZ = SOURCE_RATE / 168.0
_MAX_TICK_BUDGET = 16          # ~320 ms; absorbs jitter, caps catch-up after a stall


def _to_float(pcm: bytes) -> np.ndarray:
    """PRE2 8-bit PCM -> DC-free float32.

    The recovered volume table is linear (``out = sample * volume / 64``) and the faithful
    mixer sums several such channels onto a 0-based buffer, so the audible (AC) content of one
    channel is ``(sample - mean) * volume / 64`` -- the per-sample DC is just an offset that
    appears as the buffer's centre and is inaudible. PRE2 samples are *not* a fixed-centre
    format (their rest byte is 0 on some instruments, 0x20 on others), so the only correct,
    universal centring is to remove each sample's own DC. (Treating them as unsigned centred
    at 0x80 -- the old bug -- shoved the whole waveform to a huge negative offset.)"""
    a = np.frombuffer(pcm, dtype=np.uint8).astype(np.float32)
    if a.size:
        a = a - a.mean()
    return a / 128.0


class _Voice:
    """One channel rendered in float, rooted in a recovered ``TrackerVoice`` (read by index).

    Holds only the *enhanced* render position (a float sample cursor); the note/pitch/volume
    come live from the recovered voice each chunk, so volume slides and effects carry through
    without re-triggering. A retrigger (reported by ``advance_tick``) resets the cursor to 0."""

    __slots__ = ("data", "pos", "active", "loop_start", "loop_end", "looping", "length")

    def __init__(self) -> None:
        self.data = np.zeros(0, np.float32)
        self.pos = 0.0
        self.active = False
        self.looping = False
        self.loop_start = 0.0
        self.loop_end = 0.0
        self.length = 0

    def trigger(self, instr) -> None:
        """(Re)start this voice on a recovered :class:`Instrument` (sample bytes + loop)."""
        if instr is None or not instr.sample:
            self.active = False
            return
        self.data = _to_float(instr.sample)
        self.length = len(self.data)
        self.pos = 0.0
        self.active = self.length > 1
        self.looping = instr.loop_len >= 3
        if self.looping:
            self.loop_start = float((instr.loop_start - instr.ptr_off) & 0xFFFF)
            self.loop_end = self.loop_start + float(instr.loop_len)
        else:
            self.loop_start = 0.0
            self.loop_end = float(self.length)

    def render_into(self, out: np.ndarray, advance: float, gain: float) -> None:
        """Add ``len(out)`` mono samples at the recovered pitch (``advance`` src-samples per
        output sample) and volume ``gain``, advancing + looping the float cursor."""
        n = len(out)
        if not self.active or self.length <= 1 or advance <= 0.0:
            return
        raw = self.pos + advance * np.arange(n, dtype=np.float64)
        end = self.pos + advance * n
        if self.looping and self.loop_end > self.loop_start:
            span = self.loop_end - self.loop_start
            over = raw >= self.loop_end
            if over.any():
                raw = np.where(over, self.loop_start + np.mod(raw - self.loop_start, span), raw)
            if end >= self.loop_end:
                end = self.loop_start + (end - self.loop_start) % span
            self.pos = end
        else:
            valid = raw < self.length
            n_valid = int(valid.sum())
            if n_valid < n:
                raw = raw[:n_valid]              # one-shot ran out mid-chunk
                self.active = False
            self.pos = end
            if n_valid == 0:
                return
        i0 = raw.astype(np.int64)
        np.clip(i0, 0, self.length - 1, out=i0)
        i1 = np.minimum(i0 + 1, self.length - 1)
        frac = (raw - i0).astype(np.float32)
        seg = self.data[i0] * (1.0 - frac) + self.data[i1] * frac
        out[: len(seg)] += seg * gain


class _SfxVoice:
    """A one-shot SFX rendered in float (pure audio time, not sequencer-gated)."""

    __slots__ = ("data", "pos", "advance", "gain", "active")

    def __init__(self, ev: PlaySfx, out_rate: int) -> None:
        self.data = _to_float(ev.pcm)
        self.pos = 0.0
        self.advance = (ev.source_rate or SFX_SAMPLE_RATE) / out_rate
        self.gain = min(ev.volume, VOL_MAX) / VOL_MAX
        self.active = len(self.data) > 1

    def render_into(self, out: np.ndarray) -> None:
        n = len(out)
        if not self.active:
            return
        raw = self.pos + self.advance * np.arange(n, dtype=np.float64)
        self.pos = self.pos + self.advance * n
        valid = raw < len(self.data)
        nv = int(valid.sum())
        if nv < n:
            self.active = False
        if nv == 0:
            return
        raw = raw[:nv]
        i0 = raw.astype(np.int64)
        i1 = np.minimum(i0 + 1, len(self.data) - 1)
        frac = (raw - i0).astype(np.float32)
        out[:nv] += (self.data[i0] * (1.0 - frac) + self.data[i1] * frac) * self.gain


class EnhancedRenderer:
    """Modern float renderer over a :class:`RecoveredAudioSystem` (the recovered model).

    ``free_run=True`` ticks at the song's own tempo (offline rendering); otherwise the
    sequencer advances only on supplied game-audio ticks (:meth:`advance_ticks`)."""

    def __init__(self, system: RecoveredAudioSystem, out_rate: int = OUT_RATE,
                 *, free_run: bool = False) -> None:
        self.sys = system
        self.out_rate = out_rate
        self._free_run = free_run
        self._voices = [_Voice() for _ in range(NUM_VOICES)]
        self._sfx: list[_SfxVoice] = []
        # Headroom: each DC-free channel peaks near +-0.25 (the vol_table's 1/4-of-range scale),
        # so 4 channels sum to about unity; a master near 1 keeps a full mix just under the
        # limiter knee instead of slamming it.
        self._music_gain = 1.3
        self._sfx_gain = 0.6
        # Light stereo image (Amiga-style: channels 0,3 left / 1,2 right, softened to 70/30 so
        # it stays mono-compatible). The faithful path is mono; this is an enhanced-only nicety.
        self._pan_left = (0.70, 0.30, 0.30, 0.70)
        self._samples_per_tick = out_rate / TICK_HZ
        self._samples_to_tick = 0.0
        self._tick_budget = 0
        # Diagnostics (native tick cadence): a healthy free-run renderer ticks the recovered
        # sequencer at ~TICK_HZ relative to the frames it renders. A drift means the audio
        # clock and the sequencer disagree (the bug class behind "tempo changes").
        self.ticks_rendered = 0
        self.frames_rendered = 0

    # -- clocks ---------------------------------------------------------------
    def advance_ticks(self, k: int) -> None:
        """Supply ``k`` ticks of game audio time (1 recovered block == 1 sequencer tick)."""
        if not self._free_run and k > 0:
            self._tick_budget = min(self._tick_budget + k, _MAX_TICK_BUDGET)

    def _sequencer_tick(self) -> None:
        """One shared sequencer tick: advance the recovered system + react to retriggers."""
        triggered = self.sys.advance_tick()
        self.ticks_rendered += 1
        for i in triggered:
            self._voices[i].trigger(self.sys.mixer_instrument(self.sys.voices[i].instrument))

    def tick_cadence_hz(self) -> float:
        """The realised sequencer rate per rendered audio time (should track ``TICK_HZ``)."""
        if self.frames_rendered <= 0:
            return 0.0
        return self.ticks_rendered * self.out_rate / self.frames_rendered

    def _pitch_advance(self, period: int) -> float:
        """Recovered resample step -> float source-samples consumed per output sample.

        The recovered mixer advances the source by ``1 + period/256`` per *faithful* output
        sample (base lodsb + fractional step) at ``SOURCE_RATE``; the enhanced renderer plays
        that same pitch at ``out_rate``."""
        return (1.0 + period / 256.0) * SOURCE_RATE / self.out_rate

    # -- output ---------------------------------------------------------------
    def render(self, n_frames: int) -> np.ndarray:
        """Render ``n_frames`` of float32 stereo ``(n, 2)``, driven by audio time."""
        out = np.zeros((n_frames, 2), np.float32)
        self.frames_rendered += n_frames
        # SFX: spawn float one-shots from the recovered command queue (pure audio time).
        for ev in self.sys.drain_sfx():
            self._sfx.append(_SfxVoice(ev, self.out_rate))

        left = np.zeros(n_frames, np.float32)
        right = np.zeros(n_frames, np.float32)
        tmp = np.zeros(n_frames, np.float32)
        i = 0
        while i < n_frames:
            if self._samples_to_tick <= 0.0:
                if self._free_run or self._tick_budget > 0:
                    self._sequencer_tick()
                    if not self._free_run:
                        self._tick_budget -= 1
                self._samples_to_tick += self._samples_per_tick
            chunk = min(n_frames - i, max(1, int(self._samples_to_tick)))
            voices = self.sys.voices
            if self.sys.music_on and voices:
                for vi in range(min(NUM_VOICES, len(voices))):
                    rv = voices[vi]
                    fv = self._voices[vi]
                    if not fv.active:
                        continue
                    gain = (rv.volume / VOL_MAX) if rv.volume <= VOL_MAX else 1.0
                    seg = tmp[:chunk]
                    seg[:] = 0.0
                    fv.render_into(seg, self._pitch_advance(rv.period), gain)
                    lf = self._pan_left[vi]
                    left[i:i + chunk] += seg * lf
                    right[i:i + chunk] += seg * (1.0 - lf)
            self._samples_to_tick -= chunk
            i += chunk
        out[:, 0] += left * self._music_gain
        out[:, 1] += right * self._music_gain

        if self._sfx:
            sl = np.zeros(n_frames, np.float32)
            for v in self._sfx:
                v.render_into(sl)
            out[:, 0] += sl * self._sfx_gain
            out[:, 1] += sl * self._sfx_gain
            self._sfx = [v for v in self._sfx if v.active]

        # Gentle soft-knee limiter: linear below ~0.7, tanh only catches the rare overshoot
        # (the headroom above keeps a normal mix well under 1.0, so this stays transparent).
        np.clip(out, -3.0, 3.0, out=out)
        knee = 0.7
        over = np.abs(out) > knee
        out[over] = np.sign(out[over]) * (knee + (1.0 - knee) * np.tanh((np.abs(out[over]) - knee) / (1.0 - knee)))
        return out
