"""VM-independent audio assets — the neutral song/sample data both backends consume.

These carry the *original* sample/module data extracted from the game, but in a
form that knows nothing about the DOS mixer's internals (no segment:offset, no
fill-buffer, no DMA). The faithful backend rebuilds the recovered mixer/tracker
structs from these; the enhanced backend reads the raw PCM + musical parameters.

8-bit PCM convention: bytes are the original **unsigned** 8-bit samples (centre
≈ 0x80), exactly as the SB DMA streamed them. Conversion to signed/float is each
backend's choice (the faithful one keeps 8-bit; the enhanced one lifts to float).
"""
from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["SOURCE_RATE", "SampleAsset", "Module"]

# The SB DMA / mixer output rate the original samples were authored against
# (sample_rate = 1_000_000 / (256 - time_constant); ~8403 Hz for PRE2). Used by the
# enhanced backend to derive per-note playback frequency; the faithful backend works
# in blocks at this rate intrinsically.
SOURCE_RATE = 8403


@dataclass(frozen=True)
class SampleAsset:
    """One instrument's PCM + loop, indexed from 0 (normalised: loop is relative)."""
    pcm: bytes            # 8-bit unsigned PCM, sample[0..]
    length: int           # play extent (the channel "end" before loop/stop)
    loop_start: int       # loop wrap index into pcm (relative); used when loop_len >= 3
    loop_len: int
    default_volume: int   # 0..0x40

    @property
    def loops(self) -> bool:
        return self.loop_len >= 3


@dataclass(frozen=True)
class Module:
    """A complete song asset: order, patterns, instruments + the pitch table.

    ``period_table`` maps a note period to the mixer's resample *step* (advance/256
    per output sample at :data:`SOURCE_RATE`); both backends use it — the faithful
    one as the literal step, the enhanced one to derive playback Hz. ``vol_table`` is
    the original 8-bit volume-scaling table the faithful mixer needs; the enhanced
    backend ignores it (it scales in float)."""
    order: tuple[int, ...]              # order position -> pattern index
    song_length: int                    # last valid order index
    patterns: dict[int, bytes]          # pattern index -> 1024-byte pattern (4ch x 64 x 4)
    samples: tuple[SampleAsset, ...]    # by (sample number - 1)
    period_table: tuple[int, ...]       # note period -> resample step
    vol_table: bytes = b""              # original 8-bit volume table (faithful only)
    initial_speed: int = 6              # ticks per row at song start
    source_rate: int = SOURCE_RATE

    def step_to_hz(self, step: int) -> float:
        """Playback frequency for a note whose resample step is ``step``.

        The faithful mixer advances the source by ``step/256`` source-samples per
        output sample at ``source_rate``; that is the sample being played at
        ``source_rate * step / 256`` Hz."""
        return self.source_rate * step / 256.0
