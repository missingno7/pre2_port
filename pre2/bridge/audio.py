"""Memory views for the audio mixer (VM memory ⇄ recovered audio dataclasses).

The one place that knows *where* the PRE2 software-mixer state lives in memory.
Mixer logic + the data model live in ``pre2/recovered/mixer.py``; this only
translates layout. Factual naming. Layout from the ASM mixer (``1030:216B`` +
the ISR ``2029``) — see the ledger "Audio mixer" section. (Generic SB/DMA/PIC
hardware stays in ``dos_re``.)
"""
from __future__ import annotations

from pre2.recovered.mixer import BLOCK_LEN, ChannelState, Instrument, Sfx

DATA_SEG = 0x1A13
CODE_SEG = 0x1030

# per-channel state — arrays of 4 words, indexed by channel*2 (ds=1A13)
CH_POS = 0xB88      # sample position (0xFFFF = channel off)
CH_END = 0xB90      # sample end (relative to the instrument sample base)
CH_INSTR = 0xB98    # instrument index
CH_PERIOD = 0xBA8   # resample step
CH_VOL = 0xBB8      # volume (row = volume<<6 into the volume table)
CH_FRAC = 0xBC8     # fractional position accumulator

# instrument table: instr*16 + base
INSTR_LOOP_START = 0xBD4   # [instr*16 + 0xBD4]
INSTR_LOOP_LEN = 0xBD6     # [instr*16 + 0xBD6]
INSTR_PTR_OFF = 0xBD8      # [instr*16 + 0xBD8] sample data offset
INSTR_PTR_SEG = 0xBDA      # [instr*16 + 0xBDA] sample data segment

VOLUME_TABLE = 0x12BD      # xlatb base: scaled = [VOLUME_TABLE + (vol<<6) + sample_byte]
VOLUME_TABLE_BYTES = 65 * 64 + 256   # covers volume 0..64 rows (<<6) + a full sample byte

NUM_CHANNELS = 4

# block fill target (ds/es=1A13, di=[0x10C1]) + the music-off flag (cs:[3] bit 0x40)
VAR_FILL_BUF = 0x10C1
MUSIC_FLAG = (CODE_SEG, 0x0003, 0x40)   # music OFF when cs:[3] & 0x40
# SFX overlay state
SFX_SRC_OFF = 0x1002
SFX_REMAINING = 0x1004
SFX_SEG_PTR = 0x0B57


def _rw(mem, seg, off):
    b = ((seg << 4) + off) & 0xFFFFF
    return mem.data[b] | (mem.data[b + 1] << 8)


def read_channel(mem, ch: int) -> ChannelState:
    i = ch * 2
    return ChannelState(
        pos=_rw(mem, DATA_SEG, CH_POS + i), end=_rw(mem, DATA_SEG, CH_END + i),
        instrument=_rw(mem, DATA_SEG, CH_INSTR + i), period=_rw(mem, DATA_SEG, CH_PERIOD + i),
        volume=_rw(mem, DATA_SEG, CH_VOL + i), frac=_rw(mem, DATA_SEG, CH_FRAC + i),
    )


def read_instrument(mem, instr: int, channel_end: int) -> Instrument:
    base = instr * 16
    loop_start = _rw(mem, DATA_SEG, INSTR_LOOP_START + base)
    loop_len = _rw(mem, DATA_SEG, INSTR_LOOP_LEN + base)
    seg = _rw(mem, DATA_SEG, INSTR_PTR_SEG + base)
    off = _rw(mem, DATA_SEG, INSTR_PTR_OFF + base)
    flat = ((seg << 4) + off) & 0xFFFFF
    want = max(channel_end, loop_start + loop_len) + BLOCK_LEN + 8  # cover end + overshoot
    return Instrument(loop_start=loop_start, loop_len=loop_len,
                      sample=bytes(mem.data[flat:flat + want]))


def read_sfx(mem) -> Sfx:
    pos = _rw(mem, DATA_SEG, SFX_SRC_OFF)
    remaining = _rw(mem, DATA_SEG, SFX_REMAINING)
    seg = _rw(mem, DATA_SEG, SFX_SEG_PTR)
    flat = ((seg << 4) + pos) & 0xFFFFF
    return Sfx(pos=pos, remaining=remaining,
               sample=bytes(mem.data[flat:flat + min(remaining, BLOCK_LEN)]))


def read_volume_table(mem) -> bytes:
    base = ((DATA_SEG << 4) + VOLUME_TABLE) & 0xFFFFF
    return bytes(mem.data[base:base + VOLUME_TABLE_BYTES])


def music_on(mem) -> bool:
    seg, off, bit = MUSIC_FLAG
    return not (mem.data[((seg << 4) + off) & 0xFFFFF] & bit)


def fill_buffer_flat(mem) -> int:
    """Flat address of the 168-byte block the mixer fills (1A13:[0x10C1])."""
    return ((DATA_SEG << 4) + _rw(mem, DATA_SEG, VAR_FILL_BUF)) & 0xFFFFF


def read_block(mem) -> bytearray:
    f = fill_buffer_flat(mem)
    return bytearray(mem.data[f:f + BLOCK_LEN])
