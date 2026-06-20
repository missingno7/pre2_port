"""Memory views for the audio mixer (VM memory ⇄ recovered audio dataclasses).

The one place that knows *where* the PRE2 software-mixer state lives in memory.
Mixer logic lives in ``pre2/recovered/mixer.py``; this only translates layout.
Factual naming. Layout from the ASM mixer (``1030:216B``) — see the ledger
"Audio mixer" section. (Generic SB/DMA/PIC hardware stays in ``dos_re``.)
"""
from __future__ import annotations

from dataclasses import dataclass

DATA_SEG = 0x1A13

# per-channel state — arrays of 4 words, indexed by channel*2 (ds=1A13)
CH_POS = 0xB88      # sample position (0xFFFF = channel off)
CH_END = 0xB90      # sample end (absolute, relative to sample base)
CH_INSTR = 0xB98    # instrument index
CH_PERIOD = 0xBA8   # resample step, 8.8 fixed-point (high byte = whole samples/out)
CH_VOL = 0xBB8      # volume index (<<5 = row into the volume table)
CH_FRAC = 0xBC8     # fractional position accumulator (low byte)

# instrument table: instr*16 + base; far sample ptr + loop
INSTR_BASE = 0xBD4
INSTR_LOOP_START = 0xBD4   # [instr*16 + 0xBD4]
INSTR_LOOP_LEN = 0xBD6     # [instr*16 + 0xBD6]
INSTR_PTR_OFF = 0xBD8      # [instr*16 + 0xBD8] sample data offset
INSTR_PTR_SEG = 0xBDA      # [instr*16 + 0xBDA] sample data segment

VOLUME_TABLE = 0x12BD      # xlatb base: scaled = [VOLUME_TABLE + (vol<<5) + sample_byte]
VOLUME_TABLE_BYTES = 64 * 32 + 256   # generous: covers vol 0..63 rows + a full byte

NUM_CHANNELS = 4
BLOCK_LEN = 0xA8           # 168 bytes/block

# DMA double-buffer descriptors + the fill target ([0x10C1] = next buffer to mix)
VAR_FILL_BUF = 0x10C1
# SFX overlay state
SFX_SRC_PTR = 0x1002      # active sample source offset
SFX_REMAINING = 0x1004    # remaining sample bytes
SFX_SEG = 0x0B57          # sample segment


def _rw(mem, off):
    b = ((DATA_SEG << 4) + off) & 0xFFFFF
    return mem.data[b] | (mem.data[b + 1] << 8)


@dataclass(frozen=True)
class ChannelState:
    pos: int        # sample position (0xFFFF = off)
    end: int        # sample end
    instrument: int
    period: int     # 8.8 step
    volume: int
    frac: int       # fractional accumulator

    @property
    def active(self) -> bool:
        return self.pos != 0xFFFF


@dataclass(frozen=True)
class Instrument:
    loop_start: int
    loop_len: int
    sample: bytes   # sample PCM bytes from the far pointer (length >= max played offset)


def read_channel(mem, ch: int) -> ChannelState:
    i = ch * 2
    return ChannelState(
        pos=_rw(mem, CH_POS + i), end=_rw(mem, CH_END + i),
        instrument=_rw(mem, CH_INSTR + i), period=_rw(mem, CH_PERIOD + i),
        volume=_rw(mem, CH_VOL + i), frac=_rw(mem, CH_FRAC + i),
    )


def read_instrument(mem, instr: int, want_bytes: int) -> Instrument:
    base = instr * 16
    seg = _rw(mem, INSTR_PTR_SEG + base)
    off = _rw(mem, INSTR_PTR_OFF + base)
    flat = ((seg << 4) + off) & 0xFFFFF
    return Instrument(
        loop_start=_rw(mem, INSTR_LOOP_START + base),
        loop_len=_rw(mem, INSTR_LOOP_LEN + base),
        sample=bytes(mem.data[flat:flat + max(0, want_bytes)]),
    )


def read_volume_table(mem) -> bytes:
    base = ((DATA_SEG << 4) + VOLUME_TABLE) & 0xFFFFF
    return bytes(mem.data[base:base + VOLUME_TABLE_BYTES])


def read_fill_buffer_offset(mem) -> int:
    return _rw(mem, VAR_FILL_BUF)


def read_sfx_state(mem) -> tuple[int, int, int]:
    """(source offset, remaining bytes, sample segment) of the active SFX, if any."""
    return _rw(mem, SFX_SRC_PTR), _rw(mem, SFX_REMAINING), _rw(mem, SFX_SEG)
