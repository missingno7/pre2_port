"""The DGROUP view layer (pre2/bridge/dgroup_view.py): the two backends + struct/array composition.

Proves the ports-and-adapters seam that the state-decoupling refactor rests on — the SAME view runs over a
byte-backed image (verification = memcmp) and over a read-through overlay (contract-returning islands), and
struct-of-array views compose over either.
"""
from __future__ import annotations

from pre2.views.dgroup_view import (DGROUP_BASE, ByteBackend, DgroupView, OverlayBackend, StructArray,
                                     StructView, _S16, _U8, _U16)


class _Slot(StructView):
    __slots__ = ()
    x  = _S16(0)
    y  = _S16(2)
    id = _U16(4)
    hp = _U8(6)


class _World(DgroupView):
    __slots__ = ()
    frame = _U16(0x100)
    slots = StructArray(0x200, 8, 4, _Slot)   # 4 slots, stride 8, based at DGROUP offset 0x200


def test_byte_backend_scalars_and_structs():
    d = bytearray(0x100000)
    w = _World(d)
    w.frame = 0x1234
    w.slots[2].x = -5
    w.slots[2].id = 0xBEEF
    w.slots[2].hp = 7
    # reads go straight through the image
    assert w.frame == 0x1234
    assert w.slots[2].x == -5 and w.slots[2].id == 0xBEEF and w.slots[2].hp == 7
    # ... and land at the real DGROUP offsets (byte-exact backing store preserved)
    assert d[DGROUP_BASE + 0x100] == 0x34 and d[DGROUP_BASE + 0x101] == 0x12
    base = DGROUP_BASE + 0x200 + 2 * 8
    assert d[base] == 0xFB and d[base + 1] == 0xFF            # -5 little-endian
    assert d[base + 4] == 0xEF and d[base + 5] == 0xBE        # id
    # negative index wraps
    assert w.slots[-2] is not None and w.slots[-2].id == 0xBEEF


def test_overlay_reads_through_and_accumulates():
    base_mem = {0x100: 0x40, 0x101: 0x00, 0x210: 0x09}       # the ORIGINAL DS bytes (by offset)
    ov = OverlayBackend(lambda o: base_mem.get(o & 0xFFFF, 0))
    w = _World(ov)
    assert w.frame == 0x40                                    # falls through to the base
    w.frame = 0x1234                                          # write accumulates in the contract only
    assert w.frame == 0x1234                                  # a later read SEES the accumulated write
    assert ov.writes == {0x100: 0x34, 0x101: 0x12}           # keyed by DGROUP offset
    assert base_mem[0x100] == 0x40                            # base is UNTOUCHED (pure transform)


def test_struct_array_over_overlay_targets_the_right_offsets():
    ov = OverlayBackend(lambda o: 0)
    w = _World(ov)
    w.slots[1].y = 0x0102                                     # slot 1 -> base 0x200 + 8, field y at +2
    assert ov.writes == {0x20A: 0x02, 0x20B: 0x01}
    # the same view abstraction, whichever backend — no offset appears in the caller
    assert w.slots[1].y == 0x0102


def test_byte_and_overlay_agree_on_a_read():
    d = bytearray(0x100000)
    _World(d).slots[3].id = 0x1357
    off = 0x200 + 3 * 8 + 4
    byte_view = _World(d)
    ov_view = _World(OverlayBackend(lambda o: d[DGROUP_BASE + (o & 0xFFFF)]))
    assert byte_view.slots[3].id == ov_view.slots[3].id == 0x1357
