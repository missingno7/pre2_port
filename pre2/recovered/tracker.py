"""Prehistorik 2 music tracker / sequencer — recovered native logic (pure).

Recovers the per-tick MOD sequencer at ``1030:227C`` (+ the per-channel note
processor ``22AF`` and its effect handlers) that advances the song and sets up the
per-channel playback state the mixer (``pre2/recovered/mixer.py``) consumes. Audio
Layer 3. Pure: no CPU/VM/register/SoundBlaster imports; the memory layout lives in
``pre2/bridge/audio.py``.

Per audio block the driver calls this once (when music is on). Each tick it applies
the per-channel volume slides; every ``speed`` ticks it processes the next pattern
row — decoding each channel's 4-byte note cell (PRE2's in-memory format): a sample
number triggers the note (pos=0, end=instrument length, volume=default), a period
sets the resample step via the period→step table, and the effect is applied.

PRE2 uses only five MOD effects (the rest are no-ops):
* ``A`` volume slide (``Axy`` — x up / y down, persists for the row),
* ``B`` position jump, ``C`` set volume, ``D`` pattern break, ``F`` set speed.
"""
from __future__ import annotations

from dataclasses import dataclass

from pre2.islands import oracle_link

__all__ = ["PlaybackState", "TrackerVoice", "TrackerInstrument", "tracker_tick", "VOL_MAX", "NUM_VOICES"]

VOL_MAX = 0x40       # max channel volume
NUM_VOICES = 4
ROWS_PER_PATTERN = 0x40
CELL_BYTES = 4


@dataclass
class PlaybackState:
    tick: int          # [0xB83] tick countdown to the next row
    speed: int         # [0xB82] ticks per row
    order_pos: int     # [0xB84] position in the order table
    row: int           # [0xB86] current pattern row (0..63)


@dataclass
class TrackerVoice:
    """The per-channel state the tracker maintains (a superset of the mixer's:
    pos/end/instrument/period/volume/frac, plus the tracker-only fields)."""
    pos: int           # [0xB88]
    end: int           # [0xB90]
    instrument: int    # [0xB98]
    period: int        # [0xBA8] resample step (the mixer's CH_PERIOD)
    volume: int        # [0xBB8]
    frac: int          # [0xBC8]
    volume_slide: int  # [0xBC0] per-tick volume delta
    note_period: int   # [0xBB0] the raw note value
    effect: int        # [0xBA0] (cmd << 8) | param


@dataclass(frozen=True)
class TrackerInstrument:
    length: int           # [instr*16 + 0xBD0]
    default_volume: int   # [instr*16 + 0xBD2]


def _apply_effect(v: TrackerVoice, cmd: int, param: int, pb: PlaybackState) -> None:
    if cmd == 0x0A:                                   # volume slide (handler 232F)
        up = param >> 4
        v.volume_slide = up if up != 0 else (-(param) & 0xFFFF)  # neg ax (low nibble down)
    elif cmd == 0x0B:                                 # position jump (2348)
        pb.order_pos = param
        pb.row = 0xFFFF                               # ++ below -> row 0
    elif cmd == 0x0C:                                 # set volume (2354)
        v.volume = min(param, VOL_MAX)
    elif cmd == 0x0D:                                 # pattern break (2362)
        pb.row = (param - 1) & 0xFFFF
        pb.order_pos += 1
    elif cmd == 0x0F:                                 # set speed (236D)
        pb.speed = param
    # 0..9 and E are no-ops (handler 232E = ret)


def _process_note(v: TrackerVoice, cell, instruments, period_table, pb: PlaybackState) -> None:
    v.volume_slide = 0                                # [22AF: [0xBC0]=0]
    b0, b1, b2, b3 = cell[0], cell[1], cell[2], cell[3]
    if (b0 | b1 | b2 | b3) == 0:                      # empty cell -> nothing (slide cleared)
        return
    effect_cmd = b2 & 0x0F                            # [22C2-22DA]
    effect_param = b3
    v.effect = (effect_cmd << 8) | effect_param

    sample_num = (b2 >> 4) | (b1 & 0x10)             # PRE2 in-memory cell format
    if sample_num != 0:                              # trigger the note [22E2-2305]
        instr = instruments[sample_num - 1]
        v.instrument = sample_num - 1
        v.pos = 0
        v.end = instr.length
        v.volume = instr.default_volume
        v.frac = 0

    period = (b0 | (b1 << 8)) & 0x7FFF               # [230B-231C]
    if period != 0:
        v.note_period = period
        v.period = period_table[period]              # period->step table (0xEB9)

    _apply_effect(v, effect_cmd, effect_param, pb)   # [2320-232A: jmp [bx*2+0xB62]]


@oracle_link("1030:227C",
             "advance the MOD song one sequencer tick: apply per-channel volume slides, and "
             "every `speed` ticks process the current pattern row (4 channels -> note triggers, "
             "period/effect) and advance row/order — updating playback + per-voice state",
             "VERIFIED", merge_target="audio tracker")
def tracker_tick(pb: PlaybackState, voices, pattern: bytes, order_table,
                 song_length: int, period_table, instruments) -> None:
    """Recover ``1030:227C`` — one sequencer tick. Mutates ``pb`` and ``voices``.

    ``pattern`` is the 1024-byte pattern data for ``order_table[pb.order_pos]`` (the
    current pattern); ``order_table`` is the song order; ``period_table`` maps a note
    period to a resample step; ``instruments`` is indexed by sample number - 1.
    """
    # --- per-tick: volume slides [221C-223D] ---
    for v in voices:
        if v.volume_slide != 0:
            nv = v.volume + _signed16(v.volume_slide)
            nv = 0 if nv < 0 else min(nv, VOL_MAX)
            v.volume = nv

    # --- tick countdown [223F] ---
    pb.tick = (pb.tick - 1) & 0xFF
    if pb.tick != 0:
        return
    pb.tick = pb.speed                               # reload speed [2245-2248]

    # --- process the current row's 4 channels [2270-228A] ---
    # The row pointer is latched here, before the channel loop: a Bxx/Dxx effect on
    # an early channel sets pb.row for the *advance* step (below) but must NOT move
    # where the remaining channels of *this* row are read from (the ASM walks a fixed
    # row pointer si += 4 across the 4 channels).
    row = pb.row
    for ch in range(NUM_VOICES):
        off = row * 16 + ch * CELL_BYTES
        _process_note(voices[ch], pattern[off:off + CELL_BYTES], instruments, period_table, pb)

    # --- advance row / pattern [228D-22AC] ---
    pb.row = (pb.row + 1) & 0xFFFF
    if pb.row == ROWS_PER_PATTERN:
        nxt = pb.order_pos + 1
        pb.order_pos = nxt if song_length >= nxt else 0   # [229C: cmp song_len, order+1 / jae]
        pb.row = 0


def _signed16(x: int) -> int:
    return x - 0x10000 if x & 0x8000 else x
