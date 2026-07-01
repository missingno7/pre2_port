"""Build the global sprite bank into a NativeGameState (the VM-less counterpart of 1030:2DFA).

The recovered transform ([[pre2.recovered.sprite_bank]]) is pure; this places its output into the
NativeGameState exactly where the ASM does: the 5-plane bank at the load segment, the per-sprite far
pointers in the ``[0x5f48]`` / ``[0x62e8]`` DGROUP tables, and the bumped load top ``[0x2875]``. This is the
global ``SPRITES.SQZ`` bank the cold boot (#10) needs — distinct from the per-level ``UNION.SQZ`` bank that
``native_level_load`` already builds.
"""
from __future__ import annotations

import os

from pre2.codecs.sqz import unpack_sqz
from pre2.native.state import DATA_SEG
from pre2.recovered.sprite_bank import (
    bottom_anchor_y_offsets,
    build_sprite_bank,
    build_sprite_offset_tables,
)

_DS = DATA_SEG << 4
_TABLE = 0x7190        # the 0x7190 sprite descriptor table (static DGROUP data)
_YOFFTAB = 0x752A      # [asm 2E1F] per-sprite (x_off, y_off) draw-offset table
_OFFTAB = 0x5F48       # [asm 2F0E] per-sprite offset table
_SEGTAB = 0x62E8       # [asm 2F12] per-sprite segment table
_LOAD_TOP = 0x2875     # the bump-allocator load top
_SPRITES_SEG = 0x2DB4  # [asm 2E12] the SPRITES.SQZ load segment


def _ww(d, off: int, val: int) -> None:
    d[_DS + off] = val & 0xFF
    d[_DS + off + 1] = (val >> 8) & 0xFF


def native_build_sprite_bank(state, *, game_root: str, sprites_seg: int | None = None) -> int:
    """[asm 2dfa] Decode ``SPRITES.SQZ`` and build the 5-plane global sprite bank into ``state`` at
    ``sprites_seg`` (defaults to the current load top ``[0x2875]``), write the ``[0x5f48]`` / ``[0x62e8]``
    far-pointer tables, and bump ``[0x2875]`` by the 1.25x-expanded size. Returns the new load top.

    The ``0x7190`` descriptor table is read from ``state.data`` (static DGROUP data present from boot)."""
    d = state.data
    if sprites_seg is None:
        sprites_seg = d[_DS + _LOAD_TOP] | (d[_DS + _LOAD_TOP + 1] << 8)

    table = bytes(d[_DS + _TABLE:_DS + _TABLE + 0x400])        # enough for the 460 entries + terminator

    # [asm 2E15-2E29] the sprite-bank head's one-time Y draw-offset fixup: y_off = height - y_off per id. Without
    # it, every body sprite (player + enemies) draws one full sprite-height too LOW (the club, a zero-height
    # attachment, is unaffected). Runs ONCE here, exactly where 2DFA does it (before the demux).
    draw = bytes(d[_DS + _YOFFTAB:_DS + _YOFFTAB + 0x400])
    fixed = bottom_anchor_y_offsets(table, draw)
    d[_DS + _YOFFTAB:_DS + _YOFFTAB + len(fixed)] = fixed

    with open(os.path.join(game_root, "SPRITES.SQZ"), "rb") as f:
        decoded = unpack_sqz(f.read())

    bank = build_sprite_bank(decoded, table)                  # [asm 2EBA] mask + 4 planes per sprite
    base = sprites_seg << 4
    d[base:base + len(bank)] = bank

    offsets, segments = build_sprite_offset_tables(table, sprites_seg)
    for i, (off, seg) in enumerate(zip(offsets, segments)):   # [asm 2F0E/2F12]
        _ww(d, _OFFTAB + i * 2, off)
        _ww(d, _SEGTAB + i * 2, seg)

    count = (len(decoded) + 15) // 16                          # paragraphs the decoded data occupies
    new_top = sprites_seg + count + (count >> 2) + 1           # [asm 2E2C] count + count/4 + 1 (the 1.25x reserve)
    _ww(d, _SPRITES_SEG, sprites_seg)                          # [asm 2E12]
    _ww(d, _LOAD_TOP, new_top)                                 # [asm 2E42]
    return new_top
