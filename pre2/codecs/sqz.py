"""Prehistorik 2 ``.SQZ`` asset decompression — recovered native codec.

Status: VERIFIED (byte-for-byte against the original ASM for the LZSS path).

The original game decompresses ``.SQZ`` assets with one routine at
``1030:1240-16E3`` that contains TWO codecs selected by the file header:

* an **LZW** decoder (``1240-13F5``: clear=0x100, end=0x101, 9-12 bit codes) used
  by ``keyb`` / ``castle`` / ``present`` / ``titus`` (header ``(hdr[1]&0xF0)==0x10``);
* an **LZSS** decoder (``148F-16E3``) used by every ``b4 4c cd 21`` graphics asset
  (``back*`` / ``level*`` / ``sprites`` / ``menu*`` / ``motif`` / ``map`` / ``front`` /
  ``allfonts`` / ``union`` ...) — the hot path implemented here.

``unpack_sqz_lzss`` below is a faithful, register-free translation of the LZSS
decoder. The original is a bit-stream LZSS: a control bit selects literal byte
(bit 1) vs back-reference (bit 0); the bit accumulator is a little-endian 16-bit
word read LSB-first and refilled via ``lodsw``. Distance/length use a
variable-length encoding (see the inline ``[asm ....]`` markers, which map each
block back to the original offsets in segment 1030).
"""

from __future__ import annotations

__all__ = ["unpack_sqz_lzss", "unpack_sqz_lzw", "unpack_sqz", "SQZ_LZSS_MAGIC"]

# The 10-byte "don't-run-me" stub that prefixes every LZSS graphics asset; the
# first four bytes are ``mov ah,4Ch ; int 21h``. A 7-byte header follows
# (compressed length LE16 at +10, decompressed size at +15), then the bit-stream.
SQZ_LZSS_MAGIC = bytes.fromhex("b44ccd219d89646c7a00")
_LZSS_STREAM_OFFSET = 17
# LZW assets carry a 4-byte header (magic+size); the code stream follows.
_LZW_STREAM_OFFSET = 4


def unpack_sqz(data: bytes) -> bytes:
    """Decompress a complete ``.SQZ`` file, dispatching on the header.

    Handles the two recovered formats: the ``b4 4c`` LZSS graphics format (the
    hot path) and the LZW format (``keyb`` / ``castle`` / ``present`` / ``titus``,
    header ``(data[1] & 0xF0) == 0x10``).
    """
    if data[:10] == SQZ_LZSS_MAGIC:
        return unpack_sqz_lzss(data, _LZSS_STREAM_OFFSET)
    if (data[1] & 0xF0) == 0x10:
        return unpack_sqz_lzw(data, _LZW_STREAM_OFFSET)
    raise NotImplementedError("unrecognised SQZ header " + data[:4].hex())


def unpack_sqz_lzw(data: bytes, start: int = 4) -> bytes:
    """Decompress an LZW ``.SQZ`` stream (original codec at ``1030:1240-13F5``).

    Classic variable-width LZW: 9-12 bit codes read MSB-first from a big-endian
    bit stream; ``CLEAR=0x100`` resets the dictionary, ``END=0x101`` terminates,
    new codes begin at ``0x102``; the code width grows when the next free code
    reaches the current power-of-two threshold (``[asm 133E-1351]``).
    """
    out = bytearray()
    prefix = [0] * 4096   # [asm word table @ 0x2c10]
    suffix = [0] * 4096   # [asm byte table @ 0x1c10]
    bitpos = start * 8

    width = 9
    threshold = 0x200

    def get_code(next_code: int) -> int:
        nonlocal bitpos, width, threshold
        if threshold == next_code and width < 12:   # [asm 133E-1351] grow width
            width += 1
            threshold <<= 1
        i = bitpos >> 3
        off = bitpos & 7
        b0 = data[i]
        b1 = data[i + 1] if i + 1 < len(data) else 0
        b2 = data[i + 2] if i + 2 < len(data) else 0
        code = (((b0 << 16) | (b1 << 8) | b2) >> (24 - off - width)) & ((1 << width) - 1)
        bitpos += width
        return code

    CLEAR, END, FIRST = 0x100, 0x101, 0x102
    while True:                       # [asm 1277] (re)entry, incl. after CLEAR
        width, threshold = 9, 0x200   # [asm 1329] reset
        next_code = FIRST
        code = get_code(next_code)    # [asm 127A]
        if code == END:               # [asm 127D]
            break
        prev = code
        last_byte = code & 0xFF
        out.append(last_byte)         # [asm 1282-1288]
        while True:                   # [asm 1289] main loop
            code = get_code(next_code)
            if code == CLEAR:          # [asm 128C] -> outer reset
                break
            if code == END:            # [asm 1291 -> 130b]
                return bytes(out)
            saved = code               # [asm 1296]
            stack = []
            if code >= next_code:      # [asm 129C] KwKwK (code == next free code)
                stack.append(last_byte)
                cur = prev
            else:
                cur = code
            while cur >> 8:            # [asm 12AC] walk prefix chain
                stack.append(suffix[cur])
                cur = prefix[cur]
            stack.append(cur)          # [asm 12C1] final (first) char
            last_byte = cur            # [asm 12C4]
            out.extend(reversed(stack))  # [asm 12C7-12E0] emit reversed
            if next_code < 0x1000:     # [asm 12E2-12FE] add new entry
                suffix[next_code] = last_byte
                prefix[next_code] = prev
                next_code += 1
            prev = saved               # [asm 1302]
    return bytes(out)


def unpack_sqz_lzss(data: bytes, start: int = 0) -> bytes:
    """Decompress an LZSS ``.SQZ`` bit-stream starting at ``data[start]``.

    ``data`` is the raw compressed payload (for a ``b4 4c`` asset this begins at
    file offset 17, after the wrapper/header). Returns the decompressed bytes.
    """
    out = bytearray()
    si = start

    # [asm 14C5-14C8] prime the bit accumulator: dl = 16 bits, bp = first word.
    bp = data[si] | (data[si + 1] << 8)
    si += 2
    dl = 16

    def getbit() -> int:
        # [asm shr bp,1 ; dec dl ; refill@16E3 when empty] LSB-first; the refill
        # preserves the just-extracted bit, so it is read before topping up.
        nonlocal bp, dl, si
        cf = bp & 1
        bp >>= 1
        dl -= 1
        if dl == 0:
            bp = data[si] | (data[si + 1] << 8)
            si += 2
            dl = 16
        return cf

    while True:
        # [asm 14DC] control bit: 1 -> literal, 0 -> match.
        if getbit():
            # [asm 14E4 movsb] copy one literal byte from the stream.
            out.append(data[si])
            si += 1
            continue

        # [asm 14E7] match. Read low distance byte; bh defaults to 0xFF so the
        # 16-bit distance bx is a negative (back-reference) offset.
        b1 = getbit()
        bl = data[si]
        si += 1
        bh = 0xFF

        if b1:
            # [asm 153A] long distance: shift one extra bit into bh.
            bh = ((bh << 1) | getbit()) & 0xFF
            if not getbit():
                # [asm 154A] distance extension loop (up to 3 iterations).
                dh = 2
                cl_d = 3
                while True:
                    if getbit():            # [asm 1554 jb 1562]
                        break
                    bh = ((bh << 1) | getbit()) & 0xFF   # [asm 1556/155C]
                    dh = (dh + dh) & 0xFF                  # [asm 155E]
                    cl_d -= 1
                    if cl_d == 0:
                        break
                bh = (bh - dh) & 0xFF        # [asm 1562 sub bh,dh]

            # [asm 1564] length: unary-ish run building dh, then dh -> cl.
            dh = 2
            cl_l = 4
            jumped = False
            while True:
                dh = (dh + 1) & 0xFF        # [asm 1568 inc dh]
                if getbit():                # [asm 1570 jb 1587]
                    jumped = True
                    break
                cl_l -= 1
                if cl_l == 0:
                    break
            long_len = False
            if not jumped:
                if getbit():                # [asm 157A] bit 1 -> 157C
                    dh = (dh + 1) & 0xFF
                    if getbit():            # [asm 1584 adc dh,0]
                        dh = (dh + 1) & 0xFF
                else:                        # [asm 158C]
                    if getbit():            # [asm 1592 jb 15A7] explicit length byte
                        cl = (data[si] + 0x11) & 0xFFFF   # [asm 15A7-15AA]
                        si += 1
                        long_len = True
                    else:                    # [asm 1594] 3 raw bits + 9
                        dh = 0
                        for _ in range(3):
                            dh = ((dh << 1) | getbit()) & 0xFF
                        dh = (dh + 9) & 0xFF
            if not long_len:
                cl = dh                      # [asm 1587 mov cl,dh]
        else:
            # [asm 14F3] short distance.
            if getbit():                     # [asm 14F9 jb 1509]
                # [asm 1509] shift 3 extra bits into bh, then bh -= 1.
                for _ in range(3):
                    bh = ((bh << 1) | getbit()) & 0xFF
                bh = (bh - 1) & 0xFF
                cl = 2
            else:
                # [asm 14FB cmp bl,bh ; je 15C4] end-of-stream marker.
                if bl == bh:
                    break
                cl = 2

        # [asm 1501] copy cl bytes from the back-reference (overlap allowed).
        bx = (bh << 8) | bl
        off = bx - 0x10000 if bx >= 0x8000 else bx
        ref = len(out) + off
        for _ in range(cl):
            out.append(out[ref])
            ref += 1

    return bytes(out)
