#!/usr/bin/env python3
"""Extract PRE2.EXE from the Titus CDRUN PRE2.SQZ wrapper.

Usage:
    python extract_pre2_from_sqz.py PRE2.SQZ PRE2.EXE

This implements the alternate LZW mode used by CDRUN.COM packages.
It does not include or download any game data; run it only on your own legal copy.
"""
from pathlib import Path
import argparse

class Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def get(self) -> int:
        if self.pos >= len(self.data):
            return 0
        value = self.data[self.pos]
        self.pos += 1
        return value


def decode_lzw_alt(reader: Reader) -> bytes:
    clear_code = 0x101
    end_code = 0x100
    first = 0x102
    max_table = 0x1000

    nbit = 9
    dictionary: list[tuple[int, int, int]] = []  # prefix, postfix byte, first byte
    dict_size = first

    buf24 = (reader.get() << 16) | (reader.get() << 8) | reader.get()
    bitpos = 0
    prev = clear_code
    out = bytearray()

    while prev != end_code:
        if prev == clear_code:
            nbit = 9
            dictionary.clear()
            dict_size = first

        bitpos += nbit
        code = (buf24 >> (24 - bitpos)) & ((1 << nbit) - 1)
        buf24 = (buf24 << 8) | reader.get()
        if bitpos >= 16:
            buf24 = (buf24 << 8) | reader.get()
        buf24 &= 0xFFFFFF
        bitpos &= 7

        if code != clear_code and code != end_code:
            if code < dict_size:
                new_byte = code if code < first else dictionary[code - first][2]
            else:
                if prev == clear_code or dict_size >= max_table or code != dict_size:
                    raise ValueError("Invalid alternate LZW stream")
                new_byte = prev if prev < first else dictionary[prev - first][2]

            if prev != clear_code and dict_size < max_table:
                first_byte = prev if prev < first else dictionary[prev - first][2]
                dictionary.append((prev, new_byte, first_byte))
                dict_size += 1
                if dict_size == (1 << nbit) and nbit < 12:
                    nbit += 1

            seq = bytearray()
            cur = code
            while cur >= first:
                prefix, byte, _ = dictionary[cur - first]
                seq.append(byte)
                cur = prefix
            seq.append(cur & 0xFF)
            out.extend(reversed(seq))

        prev = code

    return bytes(out)


def unpack_pre2_sqz(data: bytes) -> bytes:
    if len(data) < 4:
        raise ValueError("Input is too short")
    expected_size = (data[0] << 16) | data[2] | (data[3] << 8)
    if data[1] != 0x10:
        raise ValueError("This extractor expects the LZW PRE2.SQZ wrapper format")

    out = decode_lzw_alt(Reader(data[4:]))
    if len(out) != expected_size:
        raise ValueError(f"Unexpected output size: got {len(out)}, expected {expected_size}")
    if out[:2] != b"MZ":
        raise ValueError("Output does not look like an MZ executable")
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input_sqz", help="Path to PRE2.SQZ")
    parser.add_argument("output_exe", help="Path to write PRE2.EXE")
    args = parser.parse_args()

    exe = unpack_pre2_sqz(Path(args.input_sqz).read_bytes())
    Path(args.output_exe).write_bytes(exe)
    print(f"Wrote {args.output_exe} ({len(exe)} bytes)")

if __name__ == "__main__":
    main()
