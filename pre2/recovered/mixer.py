"""Prehistorik 2 software audio mixer — recovered native logic (pure).

Recovers the PCM block mixer that fills the SB DMA buffer (the audio decode/mix
hot kernel; the SB/DMA/PIC *hardware* stays generic in ``dos_re``). Per the layered
audio plan this is Layer 4 — it consumes the channel/SFX playback state (set up by
the ASM tracker for now) and produces the 168-byte 8-bit PCM block.

Structure (from the ASM, ``1030:2029`` ISR mix section):
* :func:`mix_sfx` (``20AB-20F3``) writes the block base: the active SFX sample
  copied in (+ silence padding), or all-silence when no SFX is playing.
* :func:`mix_channel` (``1030:216B``) **adds** one resampled, volume-scaled MOD
  channel on top (byte-wrapping add).
* :func:`mix_block` composes them: SFX base, then channels 0-2 (and channel 3 only
  when no SFX is active — SFX borrows channel 3's slot), gated by the music flag.

Pure: no CPU/VM/register/SoundBlaster imports. Memory layout lives in
``pre2/bridge/audio.py``.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from pre2.islands import oracle_link

__all__ = [
    "ChannelState", "Instrument", "Sfx", "BLOCK_LEN", "CHANNEL_OFF",
    "mix_channel", "mix_sfx", "mix_block",
]

BLOCK_LEN = 0xA8        # 168 bytes per DMA block
CHANNEL_OFF = 0xFFFF    # sample position sentinel = channel silent
_LOOP_MIN = 0x0C        # loop length below this == "no loop" (play once)


@dataclass(frozen=True)
class ChannelState:
    pos: int        # sample position (CHANNEL_OFF = silent)
    end: int        # sample end offset
    instrument: int
    period: int     # resample step; effective advance/output = 1 + period/256
    volume: int     # row into the volume table (row = volume << 6)
    frac: int       # fractional position accumulator (low byte)

    @property
    def active(self) -> bool:
        return self.pos != CHANNEL_OFF


@dataclass(frozen=True)
class Instrument:
    loop_start: int
    loop_len: int
    sample: bytes   # PCM bytes from the instrument base (indexed by absolute offset)


@dataclass(frozen=True)
class Sfx:
    pos: int        # source offset of the active effect
    remaining: int  # bytes left to play (0 = none)
    sample: bytes   # SFX PCM bytes from ``pos``


@oracle_link("1030:216B",
             "additively mix one resampled, volume-scaled MOD channel into the 168-byte PCM "
             "block and advance its sample position/loop (pos/end/frac updated)",
             "VERIFIED", merge_target="audio mixer")
def mix_channel(buffer: bytearray, ch: ChannelState, instr: Instrument,
                vol_table: bytes, block_len: int = BLOCK_LEN) -> ChannelState:
    """Recover ``1030:216B`` — add one channel into ``buffer`` (byte-wrap add).

    Returns the channel's updated state (pos/end/frac). A silent channel
    (``pos == CHANNEL_OFF``) contributes nothing and is returned unchanged.
    """
    if ch.pos == CHANNEL_OFF:                          # [asm 2189: cmp ax,0xFFFF / je ret]
        return ch
    si = ch.pos                                        # [asm 21A7: si = base + pos]
    end = ch.end                                       # sp limit = base + end
    period = ch.period
    frac = ch.frac & 0xFFFF
    vol_row = (ch.volume << 6) & 0xFFFF                # [asm 2171-217F: dx=vol, shl x6 -> vol*64]
    sample = instr.sample

    for di in range(block_len):                        # [asm cx=0xA8]
        s = sample[si]                                 # [asm 21C2: lodsb] (si advances below)
        si += 1
        scaled = vol_table[(vol_row + s) & 0xFFFF]     # [asm 21C4: xlatb volume table]
        buffer[di] = (buffer[di] + scaled) & 0xFF      # [asm 21C5: add [di],al]
        frac = (frac + period) & 0xFFFF                # [asm 21C8: add dx,bp]
        step = (frac >> 8) & 0xFF                      # [asm 21CA: al=dh]
        frac &= 0x00FF                                 # [asm 21CC: xor dh,dh]
        if step >= 0x80:                               # [asm 21CE: cbw] sign-extend
            step -= 0x100
        si = (si + step) & 0xFFFF                      # [asm 21CF: add si,ax]
        if si > end:                                   # [asm 21D1: cmp si,sp / ja]
            break

    # --- end / loop handling [asm 21DF-2215] ---
    if end > si:                                       # [asm 21F9: cmp [end],si / ja keep]
        new_pos = si
        new_end = end
    elif instr.loop_len >= _LOOP_MIN:                  # [asm 2206: cmp ax,0xC / jb]
        new_pos = instr.loop_start                     # [asm 220B]
        new_end = (instr.loop_start + instr.loop_len) & 0xFFFF
    else:
        new_pos = CHANNEL_OFF
        new_end = end
    return replace(ch, pos=new_pos & 0xFFFF, end=new_end & 0xFFFF, frac=frac & 0xFF)


def mix_sfx(buffer: bytearray, sfx: Sfx, block_len: int = BLOCK_LEN) -> Sfx:
    """Recover ``20AB-20F3`` — write the block base: SFX sample (+ silence pad) or
    all-silence. Returns the SFX state with its source/remaining advanced."""
    n = min(sfx.remaining, block_len)
    for i in range(n):                                 # [asm rep movsw copy]
        buffer[i] = sfx.sample[i]
    for i in range(n, block_len):                      # [asm rep stosw 0 pad]
        buffer[i] = 0
    if sfx.remaining == 0:
        return sfx
    return replace(sfx, pos=(sfx.pos + n) & 0xFFFF, remaining=sfx.remaining - n,
                   sample=sfx.sample[n:])


def mix_block(buffer: bytearray, channels, instruments, vol_table: bytes,
              sfx: Sfx, music_on: bool, block_len: int = BLOCK_LEN):
    """Compose one PCM block: SFX base, then the MOD channels (channel 3 only when
    no SFX is active). Returns ``(channels, sfx)`` updated."""
    sfx = mix_sfx(buffer, sfx, block_len)
    new_channels = list(channels)
    if music_on:                                       # [asm 20F5: test cs:[3],0x40]
        n = 3 if sfx.remaining > 0 else 4              # [asm 210F: ch3 only if no SFX]
        for ch in range(n):
            new_channels[ch] = mix_channel(buffer, channels[ch], instruments[ch], vol_table, block_len)
    return new_channels, sfx
