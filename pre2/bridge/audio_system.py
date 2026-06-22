"""Reconstruct the native audio engine's input (:class:`AudioState`) from VM memory.

Read-only, one place. Reuses the per-layer readers in :mod:`pre2.bridge.audio` and bundles
a complete, detached song+SFX snapshot the recovered :class:`AudioSystem` can play with no
VM / Sound Blaster.
"""
from __future__ import annotations

from pre2.bridge import audio as _a
from pre2.recovered.audio_system import AudioState
from pre2.recovered.mixer import Sfx

_DS = _a.DATA_SEG


def _read_full_sfx(mem) -> Sfx:
    """The active SFX overlay with its *whole* remaining sample (detached playback).

    ``remaining`` ([0x1006]) == 0 means no SFX: the block base is silence and all four MOD
    channels mix. The ISR base section (20D6-210C) is the authority for these offsets."""
    remaining = _a._rw(mem, _DS, _a.SFX_REMAINING)
    if remaining == 0:
        return Sfx(pos=0, remaining=0, sample=b"")
    pos = _a._rw(mem, _DS, _a.SFX_SRC_OFF)
    seg = _a._rw(mem, _DS, _a.SFX_SEG_PTR)
    flat = ((seg << 4) + pos) & 0xFFFFF
    return Sfx(pos=pos, remaining=remaining, sample=bytes(mem.data[flat:flat + remaining]))


def valid_audio_state(mem) -> bool:
    """Whether VM memory currently holds a well-formed tracker song safe to play natively.

    Non-gameplay screens (oldies/intro/menus) and mid-transition states can leave the
    music vars garbage; running the engine on those produces noise or indexes out of
    range. This gate keeps the native engine to genuine, sane tracker music."""
    if not _a.music_on(mem):
        return False
    song_length = _a.read_song_length(mem)
    pb = _a.read_playback(mem)
    return (0 < pb.speed <= 0x20 and song_length < _a.ORDER_TABLE_LEN
            and pb.order_pos <= song_length and pb.row < 0x40)


def capture_audio_state(mem, n_instruments: int = 64) -> AudioState:
    """Snapshot the full song + SFX + playback state into a detached :class:`AudioState`."""
    order_table = _a.read_order_table(mem)
    song_length = min(_a.read_song_length(mem), len(order_table) - 1)   # bound (defensive)
    tracker_instruments = _a.read_tracker_instruments(mem, n_instruments)
    mixer_instruments = [_a.read_instrument(mem, i, tracker_instruments[i].length)
                         for i in range(n_instruments)]
    patterns: dict = {}
    for op in range(song_length + 1):
        pat = order_table[op]
        if pat not in patterns:
            patterns[pat] = _a.read_current_pattern(mem, op)
    return AudioState(
        pb=_a.read_playback(mem),
        voices=[_a.read_voice(mem, ch) for ch in range(4)],
        order_table=order_table,
        patterns=patterns,
        song_length=song_length,
        period_table=_a.read_period_table(mem),
        tracker_instruments=tracker_instruments,
        mixer_instruments=mixer_instruments,
        vol_table=_a.read_volume_table(mem),
        sfx=_read_full_sfx(mem),
        music_on=_a.music_on(mem),
    )
