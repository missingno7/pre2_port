"""Composition tests for the recovered object-update walker (`pre2/recovered/object_tick.py`).

Byte-exact ASM equivalence over whole frames is proven live by `pre2/probes/probe_object_tick_composed.py`
(L6 1349 slots, earthquake/idx6 1962 slots + 1760 globals, L7 3067 slots — zero mismatches). These pin the
composition ORDER + data flow + the 8-bit dispatch detail on a dict-backed walker memory."""
from __future__ import annotations

from pre2.recovered.object_tick import OBJ_BASE, OBJ_STRIDE, object_tick


class DictMem:
    """A minimal dict-backed WalkerMem: a flat 64K object segment plus stubbed read-only tables. The handler
    table maps an 8-bit byte-offset -> handler address (mirrors `cs:[bx + 0x6AA9]`)."""

    def __init__(self, handler_table):
        self.b = bytearray(0x10000)
        self.handler_table = handler_table   # {(idx*2)&0xFF: handler_address}
        for s in range(12):                  # all slots empty by default ([si+4] = 0xFFFF)
            self.ww(OBJ_BASE + s * OBJ_STRIDE + 4, 0xFFFF)
        self._scale = 0
        self.glb_state = {"player_x": 0, "player_y": 0, "frame": 0, "shake": 0, "a340": 0, "mode": 0,
                          "a30e": 0, "a310": 0, "bc0": 0, "bc1": 0, "bd0": 0, "ror": 0,
                          "la": 0, "lb": 0, "lc": 0, "ld": 0}

    def rb(self, o): return self.b[o & 0xFFFF]
    def rw(self, o): return self.b[o & 0xFFFF] | (self.b[(o + 1) & 0xFFFF] << 8)
    def wb(self, o, v): self.b[o & 0xFFFF] = v & 0xFF
    def ww(self, o, v): self.b[o & 0xFFFF] = v & 0xFF; self.b[(o + 1) & 0xFFFF] = (v >> 8) & 0xFF
    def read_map(self, idx): return 0
    def prop_a(self, t): return 0
    def prop_b(self, t): return 0
    def slope(self, t): return 0
    def cos_table(self, a): return 0
    def sin_table(self, a): return 0
    def tile_prop(self, tx, ty): return 0
    def scale(self): return self._scale
    def handler_addr(self, tbl): return self.handler_table.get(tbl & 0xFF, 0xE800)
    def glb(self): return dict(self.glb_state)
    def write_glb(self, g): self.glb_state.update(g)


def _put_object(mem, slot, *, id, defptr, x, y, xvel, yvel, anim_ptr, state, def1, def4):
    si = OBJ_BASE + slot * OBJ_STRIDE
    mem.ww(si, x); mem.ww(si + 2, y); mem.ww(si + 4, id); mem.ww(si + 6, defptr)
    mem.ww(si + 8, xvel); mem.ww(si + 0xA, yvel); mem.ww(si + 0xC, anim_ptr); mem.wb(si + 0xE, state)
    mem.wb(defptr + 1, def1); mem.wb(defptr + 4, def4)
    mem.ww(anim_ptr, 0x0010)        # a single (non-negative) anim frame word


def test_empty_slots_are_skipped():
    mem = DictMem({2: 0x7C8C})
    for s in range(12):
        mem.ww(OBJ_BASE + s * OBJ_STRIDE + 4, 0xFFFF)
    object_tick(mem)                # no exception, nothing to do


def test_full_pipeline_runs_velocity_anim_and_handler():
    mem = DictMem({2: 0x7C8C})      # idx1 -> despawn-only handler
    # object far from player (|dx|>0x140) so the idx1 handler despawns it -> proves the handler ran
    _put_object(mem, 0, id=0x0140, defptr=0x800, x=0x400, y=0x40, xvel=0x20, yvel=0x40,
                anim_ptr=0x900, state=1, def1=1, def4=0)
    object_tick(mem)
    si = OBJ_BASE
    assert mem.rw(si + 4) == 0xFFFF             # idx1 despawn_check fired (object was far)
    assert mem.rw(si + 0xC) == 0x902            # anim advanced the script pointer (+2)


def test_velocity_and_anim_applied_when_kept():
    mem = DictMem({2: 0x7C8C})
    _put_object(mem, 0, id=0x0140, defptr=0x800, x=0x100, y=0x100, xvel=0x20, yvel=0x40,
                anim_ptr=0x900, state=1, def1=1, def4=0)
    mem.glb_state.update(player_x=0x100, player_y=0x100)   # close -> kept
    object_tick(mem)
    si = OBJ_BASE
    assert mem.rw(si) == 0x102 and mem.rw(si + 2) == 0x104   # velocity integrated (>>4)
    assert (mem.rw(si + 4) & 0x1FFF) == ((0x10 + 0x138) & 0x1FFF)   # anim frame set


def test_dispatch_index_is_8bit_shift_masking_flag_bit():
    # def[1] = 0x81: the walker does `shl bl,1` (8-bit) -> (0x81*2)&0xFF = 2 -> idx1 (0x80 bit is a flag).
    mem = DictMem({2: 0x7C8C})      # only the idx1 slot is populated
    _put_object(mem, 0, id=0x0140, defptr=0x800, x=0x400, y=0x40, xvel=0, yvel=0,
                anim_ptr=0x900, state=1, def1=0x81, def4=0)
    object_tick(mem)
    assert mem.rw(OBJ_BASE + 4) == 0xFFFF       # dispatched to idx1 despite def[1]=0x81 -> despawned
