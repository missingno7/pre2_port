"""Standard ProTracker MOD player — modern, float32, detached from the DOS mixer.

PRE2's ``.TRK`` songs are standard ProTracker "M.K." modules (see
:mod:`pre2.codecs.audio`); the game's loader compiles them into a compact
note-index/step form for its 8-bit/8.4 kHz DMA mixer, but that is an implementation
detail of the *faithful* path. This player ignores all of it and plays the **standard
module** the way the format defines it — standard Amiga period→frequency, standard
speed/BPM tempo, signed 8-bit samples lifted to float — at any modern output rate.

It depends only on numpy + the pure asset model. No period_table, no resample "step",
no 8.4 kHz rate, no 168-byte block, no DMA/ISR. This is the enhanced backend's music
engine; it is the pressure test for "is the song a self-contained musical asset?" —
and it is, so nothing low-level leaks in.

PRE2 modules use only effects A/B/C/D/F (volume slide, position jump, set volume,
pattern break, set speed/tempo); the rest are accepted and ignored.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from pre2.codecs.audio import ModModule

__all__ = ["ModPlayer", "PAL_CLOCK"]

PAL_CLOCK = 7093789.2          # Amiga PAL: sample_rate = PAL_CLOCK / (period * 2)
DEFAULT_SPEED = 6              # ticks per row (until the song's first Fxx)
# PRE2 is NOT Amiga-CIA timed: its tracker ticks once per SB DMA block (168 bytes at the
# ~8403 Hz mixer rate), a FIXED ~50 Hz. It has no BPM concept — every Fxx sets *speed*
# (ticks/row), matching the recovered tracker (pre2.recovered.tracker effect 0x0F). So the
# only tempo control is speed, and the tick rate is constant.
PRE2_TICK_HZ = 8403 / 168     # ~50.02 Hz, the game's tracker tick (SB block cadence)
ROWS_PER_PATTERN = 64
NUM_CHANNELS = 4
VOL_MAX = 64
# ProTracker channel panning is hard L/R/R/L; soften it for headphones.
_PAN = (0.25, 0.75, 0.75, 0.25)   # 0 = full left, 1 = full right
_FADE = 64                         # per-note attack ramp (samples) to kill edge clicks


def _finetune_factor(finetune: int) -> float:
    ft = finetune - 16 if finetune > 7 else finetune     # signed nibble -8..7
    return 2.0 ** (-ft / (12.0 * 8.0))                    # finetune raises pitch


@dataclass
class _Chan:
    pan: float
    data: np.ndarray = field(default_factory=lambda: np.zeros(0, np.float32))
    pos: float = 0.0
    advance: float = 0.0
    volume: int = 0
    sample_idx: int = -1
    period: int = 0
    loop_start: int = 0
    loop_end: int = 0
    looping: bool = False
    active: bool = False
    age: int = 0
    vol_slide: int = 0          # +up / -down per tick (effect A)

    def render_into(self, left: np.ndarray, right: np.ndarray) -> None:
        if not self.active or self.advance <= 0.0 or self.data.shape[0] < 2:
            return
        n = left.shape[0]
        L = self.data.shape[0]
        end = min(self.loop_end if self.loop_end > 0 else L, L)
        pos = self.pos + self.advance * np.arange(n, dtype=np.float64)
        if self.looping and end > self.loop_start + 1:
            span = end - self.loop_start
            pos = np.where(pos < end, pos, self.loop_start + np.mod(pos - end, span))
            nxt = self.pos + self.advance * n
            self.pos = (self.loop_start + (nxt - end) % span) if nxt >= end else nxt
            gate = None
        else:
            gate = pos < (end - 1)
            pos = np.clip(pos, 0.0, end - 1.0001)
            self.pos += self.advance * n
            if self.pos >= end - 1:
                self.active = False
        i0 = pos.astype(np.int64)
        np.clip(i0, 0, L - 2, out=i0)
        frac = (pos - i0).astype(np.float32)
        samp = self.data[i0] * (1.0 - frac) + self.data[i0 + 1] * frac
        if gate is not None:
            samp *= gate
        if self.age < _FADE:
            samp *= np.clip((self.age + np.arange(n)) / _FADE, 0.0, 1.0).astype(np.float32)
        self.age += n
        g = (self.volume / VOL_MAX)
        samp *= g
        left += samp * (1.0 - self.pan)
        right += samp * self.pan


class ModPlayer:
    """Plays a standard :class:`~pre2.codecs.audio.ModModule` to float32 stereo."""

    def __init__(self, module: ModModule, out_rate: int = 44100, *,
                 channel_gain: float = 0.5, loop: bool = True) -> None:
        self.mod = module
        self.out_rate = out_rate
        self.channel_gain = channel_gain
        self.loop = loop
        # slice the concatenated PCM into per-sample float arrays (signed 8-bit)
        self._samples: list[np.ndarray] = []
        off = 0
        for s in module.samples:
            raw = module.sample_data[off:off + s.length]
            off += s.length
            a = np.frombuffer(raw, dtype=np.uint8).astype(np.int16)
            a = np.where(a >= 128, a - 256, a)
            self._samples.append((a.astype(np.float32)) / 128.0)
        self.channels = [_Chan(pan=_PAN[c]) for c in range(NUM_CHANNELS)]
        self.speed = DEFAULT_SPEED
        self.tick = 0
        self.row = 0
        self.order_pos = 0
        self._tick_samples_left = 0.0
        self._jump_order: int | None = None
        self._break_row: int | None = None
        self._ended = False

    # -- sequencer ------------------------------------------------------------
    def _pattern(self, order_pos: int) -> bytes:
        pat = self.mod.order[order_pos]
        base = pat * 1024
        return self.mod.pattern_data[base:base + 1024]

    def _process_row(self) -> None:
        pat = self._pattern(self.order_pos)
        self._jump_order = None
        self._break_row = None
        for ch in range(NUM_CHANNELS):
            cell = pat[self.row * 16 + ch * 4: self.row * 16 + ch * 4 + 4]
            if len(cell) < 4:
                continue
            b0, b1, b2, b3 = cell
            sample_num = (b0 & 0xF0) | (b2 >> 4)
            period = ((b0 & 0x0F) << 8) | b1
            eff = b2 & 0x0F
            param = b3
            c = self.channels[ch]
            c.vol_slide = 0
            if sample_num and sample_num - 1 < len(self._samples):
                c.sample_idx = sample_num - 1
                c.volume = self.mod.samples[sample_num - 1].volume
            if period and c.sample_idx >= 0:
                s = self.mod.samples[c.sample_idx]
                rate = PAL_CLOCK / (period * 2.0) * _finetune_factor(s.finetune)
                c.period = period
                c.advance = rate / self.out_rate
                c.data = self._samples[c.sample_idx]
                c.loop_start = s.loop_start
                c.loop_end = (s.loop_start + s.loop_len) if s.loop_len > 2 else s.length
                c.looping = s.loop_len > 2
                c.pos = 0.0
                c.age = 0
                c.active = len(c.data) > 1
            self._apply_tick0_effect(c, eff, param)

    def _apply_tick0_effect(self, c: _Chan, eff: int, param: int) -> None:
        if eff == 0x0C:                                   # set volume
            c.volume = min(param, VOL_MAX)
        elif eff == 0x0F:                                 # set speed (PRE2: always speed,
            self.speed = max(1, param)                    # no BPM — fixed SB-block tick)
        elif eff == 0x0B:                                 # position jump
            self._jump_order = param
        elif eff == 0x0D:                                 # pattern break (BCD row)
            self._break_row = (param >> 4) * 10 + (param & 0x0F)
        elif eff == 0x0A:                                 # volume slide (per tick)
            up, down = param >> 4, param & 0x0F
            c.vol_slide = up if up else -down

    def _tick_effects(self) -> None:
        for c in self.channels:
            if c.vol_slide:
                c.volume = max(0, min(VOL_MAX, c.volume + c.vol_slide))

    def _advance_row(self) -> None:
        if self._jump_order is not None or self._break_row is not None:
            self.order_pos = self._jump_order if self._jump_order is not None else self.order_pos + 1
            self.row = self._break_row or 0
        else:
            self.row += 1
            if self.row >= ROWS_PER_PATTERN:
                self.row = 0
                self.order_pos += 1
        if self.order_pos >= len(self.mod.order):
            if self.loop:
                self.order_pos = self.mod.restart if self.mod.restart < len(self.mod.order) else 0
            else:
                self.order_pos = len(self.mod.order) - 1
                self._ended = True

    def _do_tick(self) -> None:
        if not self.mod.order:
            return
        if self.tick == 0:
            self._process_row()
        else:
            self._tick_effects()
        self.tick += 1
        if self.tick >= self.speed:
            self.tick = 0
            self._advance_row()

    # -- output ---------------------------------------------------------------
    def render(self, n_frames: int) -> np.ndarray:
        """Render ``n_frames`` of float32 **stereo** audio, shape ``(n, 2)``."""
        out = np.zeros((n_frames, 2), np.float32)
        i = 0
        while i < n_frames:
            if self._tick_samples_left <= 0.0:
                self._do_tick()
                self._tick_samples_left += self.out_rate / PRE2_TICK_HZ
            chunk = min(n_frames - i, max(1, int(self._tick_samples_left)))
            left = out[i:i + chunk, 0]
            right = out[i:i + chunk, 1]
            for c in self.channels:
                c.render_into(left, right)
            self._tick_samples_left -= chunk
            i += chunk
        out *= self.channel_gain
        np.tanh(out, out=out)        # soft limiter, no hard-clip clicks
        return out
