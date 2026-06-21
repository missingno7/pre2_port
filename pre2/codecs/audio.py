"""Prehistorik 2 audio asset models — recovered, pure (no VM/CPU/mem).

PRE2's audio assets, once SQZ-decompressed (``pre2.codecs.sqz``), are standard
formats — this module models the *decoded* payloads:

* ``*.TRK`` — SQZ-LZSS-compressed **ProTracker "M.K." module** (4 channels, 31
  samples). Confirmed for all 12 PRE2 tracks: the layout closes exactly (title +
  31×30 sample headers + order table + signature + patterns + sample PCM ==
  decoded length).
* ``SAMPLE.SQZ`` — SQZ-"other"-compressed **raw 8-bit PCM SFX bank** (60768 bytes).
  The per-effect offset/length table lives in the game code, not the asset, so it
  is left to a later (gameplay-adjacent) island; here we model the bank as raw PCM.

Scope: this is the asset **format model only**. The game's software tracker
player / mixer (``1030:218F`` + sequencer ``227C``) and the DMA-refill ISR stay ASM
for now (see docs/pre2/symbol_ledger.md "Audio mixer"). No ``@oracle_link`` — these
are standard-format parsers, not a recovery of a specific PRE2 ASM routine.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

from pre2.codecs.sqz import unpack_sqz

__all__ = [
    "ModSample", "ModModule", "parse_mod", "load_trk",
    "SFX_SAMPLE_RATE", "SFX_BANK_BYTES",
    "MOD_TITLE_LEN", "MOD_NUM_SAMPLES", "MOD_SAMPLE_HDR_LEN",
    "MOD_ORDER_LEN", "MOD_PATTERN_BYTES",
]

# ProTracker MOD layout constants
MOD_TITLE_LEN = 20
MOD_NUM_SAMPLES = 31
MOD_SAMPLE_HDR_LEN = 30
MOD_ORDER_LEN = 128
MOD_SIGNATURE_LEN = 4
MOD_PATTERN_BYTES = 1024   # 64 rows × 4 channels × 4 bytes

# SAMPLE.SQZ PCM SFX bank (the 11 effects; per-effect table is game-side)
SFX_SAMPLE_RATE = 8000     # 8-bit PCM
SFX_BANK_BYTES = 60768     # decoded length of SAMPLE.SQZ


@dataclass(frozen=True)
class ModSample:
    """One ProTracker sample header. Lengths are exposed in **bytes** (the file
    stores them as 16-bit big-endian *word* counts)."""

    name: str
    length: int        # bytes
    finetune: int      # 0..15 (signed nibble in the file)
    volume: int        # 0..64
    loop_start: int    # bytes
    loop_len: int      # bytes (1 word == "no loop" by MOD convention)


@dataclass(frozen=True)
class ModModule:
    title: str
    samples: tuple[ModSample, ...]   # always 31
    order: tuple[int, ...]           # song positions actually played (length = song_length)
    restart: int
    signature: str                   # e.g. "M.K."
    num_patterns: int
    pattern_data: bytes              # num_patterns × MOD_PATTERN_BYTES
    sample_data: bytes               # concatenated sample PCM


def parse_mod(data: bytes) -> ModModule:
    """Parse a decoded ProTracker MOD (the payload of a decompressed ``.TRK``)."""
    title = data[:MOD_TITLE_LEN].split(b"\x00")[0].decode("latin1")

    samples = []
    off = MOD_TITLE_LEN
    for _ in range(MOD_NUM_SAMPLES):
        h = data[off:off + MOD_SAMPLE_HDR_LEN]
        name = h[:22].split(b"\x00")[0].decode("latin1", "replace")
        length = struct.unpack(">H", h[22:24])[0] * 2
        finetune = h[24] & 0x0F
        volume = h[25]
        loop_start = struct.unpack(">H", h[26:28])[0] * 2
        loop_len = struct.unpack(">H", h[28:30])[0] * 2
        samples.append(ModSample(name, length, finetune, volume, loop_start, loop_len))
        off += MOD_SAMPLE_HDR_LEN

    song_length = data[off]
    restart = data[off + 1]
    off += 2
    order = tuple(data[off:off + MOD_ORDER_LEN])
    off += MOD_ORDER_LEN
    signature = data[off:off + MOD_SIGNATURE_LEN].decode("latin1")
    off += MOD_SIGNATURE_LEN

    num_patterns = (max(order) + 1) if order else 0
    pattern_data = data[off:off + num_patterns * MOD_PATTERN_BYTES]
    off += num_patterns * MOD_PATTERN_BYTES
    sample_data = data[off:off + sum(s.length for s in samples)]

    return ModModule(
        title=title,
        samples=tuple(samples),
        order=order[:song_length],
        restart=restart,
        signature=signature,
        num_patterns=num_patterns,
        pattern_data=pattern_data,
        sample_data=sample_data,
    )


def load_trk(raw: bytes) -> ModModule:
    """Decompress a ``.TRK`` file (SQZ-LZSS) and parse it as a ProTracker module."""
    return parse_mod(unpack_sqz(raw))
