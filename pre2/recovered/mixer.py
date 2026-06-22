"""Prehistorik 2 software audio mixer — recovered native logic (pure).

Recovers the PCM block mixer that fills the SB DMA buffer (the audio decode/mix
hot kernel; the SB/DMA/PIC *hardware* stays generic in ``dos_re``). Per the layered
audio plan this is Layer 4 — it consumes the channel/SFX playback state (set up by
the ASM tracker for now) and produces the 168-byte 8-bit PCM block.

Structure (from the ASM, ``1030:20AB`` ISR mix section):
* :func:`mix_sfx` (``20AB-20F3``) writes the block base: the active SFX sample
  copied in (+ silence padding), or all-silence when no SFX is playing.
* :func:`mix_channel` (``1030:218F``) **adds** one resampled, volume-scaled MOD
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
_LOOP_MIN = 0x03        # loop_len <= 2 == "no loop" (play once) [asm 2215: cmp 2 / jbe]


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
    sample: bytes   # PCM bytes from the instrument's PTR_OFF base (indexed relative to it)
    ptr_off: int = 0  # the instrument's far-pointer offset; loop_start is absolute to the
                      # segment, so the loop wrap reads sample[loop_start - ptr_off]


@dataclass(frozen=True)
class Sfx:
    pos: int        # source offset of the active effect
    remaining: int  # bytes left to play (0 = none)
    sample: bytes   # SFX PCM bytes from ``pos``


@oracle_link("1030:218F",
             "additively mix one resampled, volume-scaled MOD channel into the 168-byte PCM "
             "block and advance its sample position/loop (pos/end/frac updated)",
             "VERIFIED", merge_target="audio mixer")
def mix_channel(buffer: bytearray, ch: ChannelState, instr: Instrument,
                vol_table: bytes, block_len: int = BLOCK_LEN) -> ChannelState:
    """Recover ``1030:218F`` — add one channel into ``buffer`` (byte-wrap add).

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

    ptr_off = instr.ptr_off
    new_pos = None                                     # set only when the channel turns off
    cx = block_len                                     # [asm 21DF: mov cx,0xA8] block counter
    di = 0                                             # [asm 21E2: di = fill buffer]
    wraps = 0                                          # loop-wrap watchdog (anti-freeze)
    while cx > 0:                                      # [asm mix loop @21E6 ... loop 21E6]
        # The ASM reads es:lodsb / xlatb at 16-bit segment offsets that wrap at 0xFFFF and
        # never fault; when the channel state is uninitialised (cold boot / a stray "on"
        # channel before a song loads) si / vol_row can index past our fetched windows.
        # Read out-of-range as 0 instead of raising -- a no-op for valid playback (si and
        # vol_row+s are always in range there, so this stays byte-exact vs the ASM).
        s = sample[si] if si < len(sample) else 0      # [asm 21E6: es:lodsb] si rel to PTR_OFF
        si = (si + 1) & 0xFFFF
        vi = (vol_row + s) & 0xFFFF                     # [asm 21E8: xlatb volume table]
        scaled = vol_table[vi] if vi < len(vol_table) else 0
        if di < block_len:                             # [asm 21E9: add [di],al] (may overrun on wrap)
            buffer[di] = (buffer[di] + scaled) & 0xFF
        di += 1                                        # [asm 21EB: inc di]
        frac = (frac + period) & 0xFFFF                # [asm 21EC: add dx,bp]
        step = (frac >> 8) & 0xFF                      # [asm 21EE: al=dh]
        frac &= 0x00FF                                 # [asm 21F0: xor dh,dh]
        if step >= 0x80:                               # [asm 21F2: cbw] sign-extend
            step -= 0x100
        si = (si + step) & 0xFFFF                      # [asm 21F3: add si,ax]
        if si > end:                                   # [asm 21F5: cmp si,sp / ja 21FD]
            if instr.loop_len >= _LOOP_MIN:            # looping [asm 2215: cmp 2 / ja]
                # wrap to loop start [asm 221A: si=[bx+0BD6]]; loop_start is an absolute
                # segment offset, sample[] is based at PTR_OFF, so subtract it. The asm
                # jmps back to 21E6 WITHOUT the `loop`, so cx is NOT decremented here.
                si = (instr.loop_start - ptr_off) & 0xFFFF
                # Anti-freeze watchdog: a valid loop wraps at most ~block_len/loop_len times
                # (cx falls between wraps), so it can never exceed block_len wraps. A
                # DEGENERATE loop region (loop_start past end, from a corrupt/garbage channel
                # state) would make the ASM spin here forever -- bound it and silence the
                # channel instead of hanging the whole game. No-op for valid loops.
                wraps += 1
                if wraps > block_len:
                    new_pos = CHANNEL_OFF
                    break
            else:
                # One-shot ran out mid-block: linear release fade over the remaining
                # block [asm 222A: al=buffer[di-1]; loop add [di],al / inc di / dec al
                # until al==0 or cx==0], then silence the channel.
                al = buffer[di - 1] if di - 1 < block_len else 0   # [asm 222A: mov al,[di-1]]
                while cx > 0 and al > 0:
                    if di < block_len:
                        buffer[di] = (buffer[di] + al) & 0xFF       # [asm 2231: add [di],al]
                    di += 1
                    al -= 1                            # [asm 2234: dec ax]
                    cx -= 1                            # [asm 2237: loop 2231]
                new_pos = CHANNEL_OFF
                break
        else:
            cx -= 1                                    # [asm 21F9: loop 21E6]

    if new_pos is None:
        # Writeback end-check [asm 2257-2277]: when the block finished (cx==0) the ASM
        # re-tests the final position against end (here si is already PTR_OFF-relative,
        # which is the ASM's si after `sub si,[bx+0BDA]` at 2257).  If it has reached/passed
        # end, a looping instrument restarts at loop_start (and end := loop_start+loop_len),
        # a one-shot goes silent; otherwise it keeps the position.  Without this, a loop that
        # lands exactly on end at block end was left at `end` instead of wrapping -> the live
        # "channel state" divergence (and, used live, the corruption that froze the game).
        if si >= end:                                  # [asm 225B: cmp end,si / jbe -> reset]
            if instr.loop_len >= _LOOP_MIN:            # [asm 2268: cmp 2 / ja -> loop]
                new_pos = instr.loop_start             # [asm 226D/2277: pos = loop_start]
                end = (instr.loop_start + instr.loop_len) & 0xFFFF   # [asm 2271-2273]
            else:
                new_pos = CHANNEL_OFF                  # [asm 2261: si = 0xFFFF]
        else:
            new_pos = si                               # [asm 2277: pos = si]
    return replace(ch, pos=new_pos & 0xFFFF, end=end & 0xFFFF, frac=frac & 0xFF)


def mix_sfx(buffer: bytearray, sfx: Sfx, block_len: int = BLOCK_LEN) -> Sfx:
    """Recover ``20AB-20F3`` — write the block base: SFX sample (+ silence pad) or
    all-silence. Returns the SFX state with its source/remaining advanced."""
    n = min(sfx.remaining, block_len, len(sfx.sample))   # never index past the SFX sample
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
