"""Memory views for the audio mixer (VM memory ⇄ recovered audio dataclasses).

The one place that knows *where* the PRE2 software-mixer state lives in memory.
Mixer logic + the data model live in ``pre2/recovered/mixer.py``; this only
translates layout. Factual naming. Layout from the ASM mixer (``1030:218F`` +
the ISR ``20AB``) — see the ledger "Audio mixer" section. (Generic SB/DMA/PIC
hardware stays in ``dos_re``.)
"""
from __future__ import annotations

from pre2.recovered.mixer import BLOCK_LEN, ChannelState, Instrument, Sfx
from pre2.recovered.tracker import PlaybackState, TrackerInstrument, TrackerVoice

DATA_SEG = 0x1A0F          # GOG build
CODE_SEG = 0x1030

# per-channel state — arrays of 4 words, indexed by channel*2 (ds=1A0F).
# GOG: channel arrays are old + 2 (verified from the mixer at 218F).
CH_POS = 0xB8A      # sample position (0xFFFF = channel off)
CH_END = 0xB92      # sample end (relative to the instrument sample base)
CH_INSTR = 0xB9A    # instrument index
CH_PERIOD = 0xBAA   # resample step
CH_VOL = 0xBBA      # volume (row = volume<<6 into the volume table)
CH_FRAC = 0xBCA     # fractional position accumulator

# instrument table: instr*16 + base. GOG: the whole instrument struct is old + 2
# (mixer loop reads loop_start [bx+0BD6], loop_len [bx+0BD8], far ptr les [bx+0BDA]).
INSTR_LOOP_START = 0xBD6   # [instr*16 + 0xBD6]
INSTR_LOOP_LEN = 0xBD8     # [instr*16 + 0xBD8]
INSTR_PTR_OFF = 0xBDA      # [instr*16 + 0xBDA] sample data offset
INSTR_PTR_SEG = 0xBDC      # [instr*16 + 0xBDC] sample data segment

VOLUME_TABLE = 0x12C1      # xlatb base: scaled = [VOLUME_TABLE + (vol<<6) + sample_byte]
VOLUME_TABLE_BYTES = 65 * 64 + 256   # covers volume 0..64 rows (<<6) + a full sample byte

NUM_CHANNELS = 4

# block fill target (ds/es=1A0F, di=[0x10C5]) + the music-off flag (cs:[3] bit 0x40)
VAR_FILL_BUF = 0x10C5
MUSIC_FLAG = (CODE_SEG, 0x0003, 0x40)   # music OFF when cs:[3] & 0x40
# SFX overlay state (from the ISR base section 20D6-210C: di=[0x10C5] fill buffer,
# cx=remaining [0x1006], si=src off [0x1004], ds=seg [0x0B59]).
SFX_SRC_OFF = 0x1004
SFX_REMAINING = 0x1006
SFX_SEG_PTR = 0x0B59


def _rw(mem, seg, off):
    b = ((seg << 4) + off) & 0xFFFFF
    return mem.data[b] | (mem.data[b + 1] << 8)


def _ww(mem, seg, off, val):
    b = ((seg << 4) + off) & 0xFFFFF
    mem.data[b] = val & 0xFF
    mem.data[b + 1] = (val >> 8) & 0xFF


def read_channel(mem, ch: int) -> ChannelState:
    i = ch * 2
    return ChannelState(
        pos=_rw(mem, DATA_SEG, CH_POS + i), end=_rw(mem, DATA_SEG, CH_END + i),
        instrument=_rw(mem, DATA_SEG, CH_INSTR + i), period=_rw(mem, DATA_SEG, CH_PERIOD + i),
        volume=_rw(mem, DATA_SEG, CH_VOL + i), frac=_rw(mem, DATA_SEG, CH_FRAC + i),
    )


def write_channel(mem, ch: int, cs: ChannelState) -> None:
    """Write back the fields 218F updates: pos (always), frac (always), end (loop).
    (Writing end unchanged when there is no loop is a no-op, matching the ASM.)"""
    i = ch * 2
    _ww(mem, DATA_SEG, CH_POS + i, cs.pos)
    _ww(mem, DATA_SEG, CH_END + i, cs.end)
    _ww(mem, DATA_SEG, CH_FRAC + i, cs.frac)


def read_instrument(mem, instr: int, channel_end: int) -> Instrument:
    base = instr * 16
    loop_start = _rw(mem, DATA_SEG, INSTR_LOOP_START + base)
    loop_len = _rw(mem, DATA_SEG, INSTR_LOOP_LEN + base)
    seg = _rw(mem, DATA_SEG, INSTR_PTR_SEG + base)
    off = _rw(mem, DATA_SEG, INSTR_PTR_OFF + base)
    flat = ((seg << 4) + off) & 0xFFFFF
    want = max(channel_end, loop_start + loop_len) + BLOCK_LEN + 8  # cover end + overshoot
    return Instrument(loop_start=loop_start, loop_len=loop_len,
                      sample=bytes(mem.data[flat:flat + want]), ptr_off=off)


def read_sfx(mem) -> Sfx:
    pos = _rw(mem, DATA_SEG, SFX_SRC_OFF)
    remaining = _rw(mem, DATA_SEG, SFX_REMAINING)
    seg = _rw(mem, DATA_SEG, SFX_SEG_PTR)
    flat = ((seg << 4) + pos) & 0xFFFFF
    return Sfx(pos=pos, remaining=remaining,
               sample=bytes(mem.data[flat:flat + min(remaining, BLOCK_LEN)]))


# --- tracker / sequencer state (1030:227C) ---
# GOG tracker (entry 227C): all PB/order/pattern/voice offsets are old + 2.
PB_SPEED = 0xB84       # ticks per row (reloads PB_TICK when it hits 0)
PB_TICK = 0xB85        # tick countdown ([asm 22A1: dec byte [0B85]])
PB_ORDER = 0xB86       # order-table position ([asm 22FA])
PB_ROW = 0xB88         # current row ([asm 22EF: inc [0B88]; cmp 0x40])
V_EFFECT = 0xBA2       # per-channel effect (cmd<<8 | param)
V_NOTE_PERIOD = 0xBB2  # raw note value
V_VOL_SLIDE = 0xBC2    # per-tick volume delta ([asm 227C: add [di+0BC2] into CH_VOL])
ORDER_TABLE = 0xDC7    # order table (pattern sequence), bytes ([asm 22B3: [bx+0DC7]])
SONG_LENGTH = 0xDC2    # number of order positions ([asm 22FE: cmp [0DC2],al])
PATTERN_SEG_BASE = 0xB5E  # pattern data: seg = [0xB5E] + pattern*64 ([asm 22BC: add ax,[0B5E]])
PATTERN_OFF = 0xDC5       #               off = [0xDC5]
INSTR_LENGTH = 0xBD2   # [instr*16 + 0xBD2]
INSTR_VOLUME = 0xBD4   # [instr*16 + 0xBD4]
PERIOD_TABLE = 0xEBB   # note period -> resample step (word array)
PERIOD_TABLE_WORDS = 0x1000
PATTERN_BYTES = 0x400  # 64 rows x 16 bytes


def _rb(mem, off):
    return mem.data[((DATA_SEG << 4) + off) & 0xFFFFF]


def _wb(mem, off, val):
    mem.data[((DATA_SEG << 4) + off) & 0xFFFFF] = val & 0xFF


def read_playback(mem) -> PlaybackState:
    return PlaybackState(tick=_rb(mem, PB_TICK), speed=_rb(mem, PB_SPEED),
                         order_pos=_rw(mem, DATA_SEG, PB_ORDER), row=_rw(mem, DATA_SEG, PB_ROW))


def write_playback(mem, pb: PlaybackState) -> None:
    _wb(mem, PB_TICK, pb.tick)
    _wb(mem, PB_SPEED, pb.speed)
    _ww(mem, DATA_SEG, PB_ORDER, pb.order_pos)
    _ww(mem, DATA_SEG, PB_ROW, pb.row)


def read_voice(mem, ch: int) -> TrackerVoice:
    i = ch * 2
    return TrackerVoice(
        pos=_rw(mem, DATA_SEG, CH_POS + i), end=_rw(mem, DATA_SEG, CH_END + i),
        instrument=_rw(mem, DATA_SEG, CH_INSTR + i), period=_rw(mem, DATA_SEG, CH_PERIOD + i),
        volume=_rw(mem, DATA_SEG, CH_VOL + i), frac=_rw(mem, DATA_SEG, CH_FRAC + i),
        volume_slide=_rw(mem, DATA_SEG, V_VOL_SLIDE + i),
        note_period=_rw(mem, DATA_SEG, V_NOTE_PERIOD + i),
        effect=_rw(mem, DATA_SEG, V_EFFECT + i),
    )


def write_voice(mem, ch: int, v: TrackerVoice) -> None:
    i = ch * 2
    _ww(mem, DATA_SEG, CH_POS + i, v.pos)
    _ww(mem, DATA_SEG, CH_END + i, v.end)
    _ww(mem, DATA_SEG, CH_INSTR + i, v.instrument)
    _ww(mem, DATA_SEG, CH_PERIOD + i, v.period)
    _ww(mem, DATA_SEG, CH_VOL + i, v.volume)
    _ww(mem, DATA_SEG, CH_FRAC + i, v.frac)
    _ww(mem, DATA_SEG, V_VOL_SLIDE + i, v.volume_slide)
    _ww(mem, DATA_SEG, V_NOTE_PERIOD + i, v.note_period)
    _ww(mem, DATA_SEG, V_EFFECT + i, v.effect)


ORDER_TABLE_LEN = 0x100   # max MOD order positions (song_length must be below this)


def read_order_table(mem) -> bytes:
    base = ((DATA_SEG << 4) + ORDER_TABLE) & 0xFFFFF
    return bytes(mem.data[base:base + ORDER_TABLE_LEN])


def read_song_length(mem) -> int:
    return _rb(mem, SONG_LENGTH)


def read_period_table(mem) -> list[int]:
    """The note-period -> resample-step table, indexable by ANY 15-bit period.

    The ASM reads it as ``[bx + 0xEBB]`` with ``bx = period*2`` (period & 0x7FFF), so the
    effective offset wraps at 0xFFFF (real-mode 16-bit addressing). We build the full
    0x8000-entry table from the whole DGROUP segment with that wrap, so the recovered
    tracker never goes out of range and matches the ASM for high periods too (the old
    fixed 0x1000-word window crashed on songs whose periods exceed it, e.g. the title music)."""
    base = (DATA_SEG << 4) & 0xFFFFF
    seg = mem.data[base:base + 0x10000]
    return [seg[(PERIOD_TABLE + 2 * p) & 0xFFFF] | (seg[(PERIOD_TABLE + 2 * p + 1) & 0xFFFF] << 8)
            for p in range(0x8000)]


def read_tracker_instruments(mem, count: int = 64) -> list[TrackerInstrument]:
    return [TrackerInstrument(length=_rw(mem, DATA_SEG, INSTR_LENGTH + n * 16),
                              default_volume=_rw(mem, DATA_SEG, INSTR_VOLUME + n * 16))
            for n in range(count)]


def read_current_pattern(mem, order_pos: int) -> bytes:
    pattern = read_order_table(mem)[order_pos]
    seg = (_rw(mem, DATA_SEG, PATTERN_SEG_BASE) + pattern * 64) & 0xFFFF
    off = _rw(mem, DATA_SEG, PATTERN_OFF)
    flat = ((seg << 4) + off) & 0xFFFFF
    return bytes(mem.data[flat:flat + PATTERN_BYTES])


def read_volume_table(mem) -> bytes:
    base = ((DATA_SEG << 4) + VOLUME_TABLE) & 0xFFFFF
    return bytes(mem.data[base:base + VOLUME_TABLE_BYTES])


def music_on(mem) -> bool:
    seg, off, bit = MUSIC_FLAG
    return not (mem.data[((seg << 4) + off) & 0xFFFFF] & bit)


def fill_buffer_flat(mem) -> int:
    """Flat address of the 168-byte block the mixer fills (1A0F:[0x10C5])."""
    return ((DATA_SEG << 4) + _rw(mem, DATA_SEG, VAR_FILL_BUF)) & 0xFFFFF


def read_block(mem) -> bytearray:
    f = fill_buffer_flat(mem)
    return bytearray(mem.data[f:f + BLOCK_LEN])
