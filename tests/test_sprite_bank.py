"""Verification test for the recovered shared sprite-bank build (``pre2.recovered.sprite_bank``).

The transform (1030:2DFA) was proven **byte-for-byte equal to the original ASM** by capturing the bank the
VM builds at ``2dfa``'s RET (from a hybrid cold-start replay) and diffing it against the recovered output:
``build_sprite_bank(unpack_sqz(SPRITES.SQZ), table)`` reproduced all 196230 sprite-data bytes (460 sprites)
exactly. The bank is allocated to a paragraph boundary, so the VM image has 26 bytes of uninitialised slack
past the last sprite — that is not sprite data and is excluded from the golden.

The golden below is the SHA-1 of the recovered bank; it was captured from the run that matched the VM, so
this locks the transform with no VM in the loop. The ``0x7190`` descriptor table (static EXE data) is the
committed fixture.
"""
from __future__ import annotations

import hashlib
import struct
from pathlib import Path

import pytest

from pre2.codecs.sqz import unpack_sqz
from pre2.recovered.sprite_bank import (
    build_sprite_bank,
    build_sprite_offset_tables,
    sprite_descriptor_sizes,
)

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
TABLE_FIXTURE = ROOT / "tests" / "fixtures" / "sprites_descriptor_table.bin"

GOLD_SPRITE_BANK = "2f656a0c738a670157a9873e5c7676e035cd4ef5"   # 196230 bytes, verified == VM bank
GOLD_OFFSETS = "3be2c768a3cd117245b7630ffa0124f48d90aca8"       # offsets at base 0 (verified == VM [0x5f48])
GOLD_SEG_DELTAS = "514c4a0e4fc4d134385734c2dbafab5a1ce4c6f2"    # segments at base 0 (verified == VM [0x62e8]-base)


def _h16(vals) -> str:
    return hashlib.sha1(struct.pack(f"<{len(vals)}H", *vals)).hexdigest()

pytestmark = pytest.mark.skipif(
    not (ASSETS / "SPRITES.SQZ").exists() or not TABLE_FIXTURE.exists(),
    reason="SPRITES.SQZ or the descriptor-table fixture not present",
)


def test_sprite_descriptor_table():
    table = TABLE_FIXTURE.read_bytes()
    sizes = sprite_descriptor_sizes(table)
    assert len(sizes) == 460                                  # 460 sprites before the 0-terminator
    # the 4-plane source is exactly sum(4*bp); the 5-plane bank is sum(5*bp)
    assert 4 * sum(sizes) == len(unpack_sqz((ASSETS / "SPRITES.SQZ").read_bytes()))


def test_build_sprite_bank_byte_exact():
    sprites = unpack_sqz((ASSETS / "SPRITES.SQZ").read_bytes())
    table = TABLE_FIXTURE.read_bytes()
    bank = build_sprite_bank(sprites, table)
    assert len(bank) == 5 * sum(sprite_descriptor_sizes(table))  # mask + 4 planes per sprite
    assert hashlib.sha1(bank).hexdigest() == GOLD_SPRITE_BANK


def test_mask_plane_is_or_of_colour_planes():
    # the leading mask plane of each sprite is the OR of its four colour planes
    sprites = unpack_sqz((ASSETS / "SPRITES.SQZ").read_bytes())
    table = TABLE_FIXTURE.read_bytes()
    sizes = sprite_descriptor_sizes(table)
    bank = build_sprite_bank(sprites, table)
    bp = sizes[0]
    mask, p0, p1, p2, p3 = (bank[0:bp], bank[bp:2*bp], bank[2*bp:3*bp], bank[3*bp:4*bp], bank[4*bp:5*bp])
    assert mask == bytes(p0[j] | p1[j] | p2[j] | p3[j] for j in range(bp))


def test_sprite_offset_tables_byte_exact():
    table = TABLE_FIXTURE.read_bytes()
    offsets, segments = build_sprite_offset_tables(table, 0)
    assert len(offsets) == len(segments) == 460
    assert _h16(offsets) == GOLD_OFFSETS
    assert _h16(segments) == GOLD_SEG_DELTAS
    # a boundary sprite's stored offset can exceed 0x1000 (the normalize happens after storing)
    assert max(offsets) >= 0x1000


def test_sprite_offset_tables_base_relative():
    # the offset table is base-independent; the segment table is just the base + the base-0 deltas
    table = TABLE_FIXTURE.read_bytes()
    off0, seg0 = build_sprite_offset_tables(table, 0)
    offb, segb = build_sprite_offset_tables(table, 0x2CD7)
    assert off0 == offb
    assert segb == [(0x2CD7 + s) & 0xFFFF for s in seg0]


def test_native_build_sprite_bank_places_bank_and_tables():
    # the native builder writes the recovered transform into a NativeGameState exactly where 2dfa does:
    # the bank at the load segment, the far-pointer tables at [0x5f48]/[0x62e8], and the 1.25x-bumped top.
    from pre2.native.sprite_bank import native_build_sprite_bank
    from pre2.native.state import DATA_SEG, NativeGameState

    ds = DATA_SEG << 4
    table = TABLE_FIXTURE.read_bytes()
    st = NativeGameState(bytearray(0x100000))
    st.data[ds + 0x7190:ds + 0x7190 + len(table)] = table          # the static descriptor table
    sseg = 0x2CD7
    new_top = native_build_sprite_bank(st, game_root=str(ASSETS), sprites_seg=sseg)

    sprites = unpack_sqz((ASSETS / "SPRITES.SQZ").read_bytes())
    bank = build_sprite_bank(sprites, table)
    offsets, segments = build_sprite_offset_tables(table, sseg)
    assert bytes(st.data[sseg << 4:(sseg << 4) + len(bank)]) == bank
    assert struct.unpack(f"<{len(offsets)}H", bytes(st.data[ds + 0x5F48:ds + 0x5F48 + 2 * len(offsets)])) == tuple(offsets)
    assert struct.unpack(f"<{len(segments)}H", bytes(st.data[ds + 0x62E8:ds + 0x62E8 + 2 * len(segments)])) == tuple(segments)
    count = (len(sprites) + 15) // 16
    assert new_top == sseg + count + (count >> 2) + 1              # the 1.25x reserve
    assert (st.data[ds + 0x2DB4] | (st.data[ds + 0x2DB4 + 1] << 8)) == sseg
