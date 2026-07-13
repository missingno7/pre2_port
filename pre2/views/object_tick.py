"""Bridge for the composed object-update walker (1030:684E..6913).

Presents the live VM memory as the :class:`~pre2.recovered.object_tick.object_tick` ``WalkerMem`` accessor —
in place (no copy): word/byte read+write in the object data segment, the level-map terrain lookups, the cos/
sin tables, the global-scale + shake/PRNG state, the AI handler-address table (``cs:[bx+0x6AA9]``), and the
effect-spawn emit (``spawn_effects`` over the ``0x7DE6`` list). Pure layout/translation — no game logic.
"""
from __future__ import annotations

from pre2.recovered.object_update import spawn_effects
from pre2.views.tables import Tables
from pre2.native.dgroup_offsets import (
    ANIM_GATE, ANIM_READY, COMBO_COMPLETE_6BE2, FIREFLY_SCRATCH_A, FIREFLY_SCRATCH_B, FRAME_TIMER, LEVEL_DATA_SEG, LEVEL_INDEX, PLAYER_SLOT, PLAYER_Y, QUAKE_DIST_HI, QUAKE_DIST_LO, RNG_ROTATE, RNG_STATE, SHAKE_MAGNITUDE)

_FX_LIST = 0x7DE6        # secondary effect list (6-byte entries) the spawning handlers emit into
_HANDLER_TABLE = 0x6AA9  # cs:[ (def[1]*2)&0xFF + 0x6AA9 ] -> AI handler address


class _Slot:
    """A writable view of one 6-byte effect entry (x word, y word, b4 byte, b5 byte) for spawn_effects."""
    __slots__ = ("mem", "off")

    def __init__(self, mem, off):
        self.mem, self.off = mem, off

    def __setitem__(self, i, v):
        if i == 0:
            self.mem.ww(self.off, v)
        elif i == 1:
            self.mem.ww(self.off + 2, v)
        elif i == 2:
            self.mem.wb(self.off + 4, v)
        elif i == 3:
            self.mem.wb(self.off + 5, v)


class LiveWalkerMem:
    """In-place WalkerMem over the live VM (`cpu.mem.data`) for the object_tick hook.

    `ds` is the object data segment, `cs` the code segment holding the handler table; `map_seg` is the level
    tilemap segment `[0x2DDA]`. Writes go straight to VM memory (no image copy)."""

    def __init__(self, cpu):
        self.data = cpu.mem.data
        self.ds = cpu.s.ds & 0xFFFF
        self.cs = cpu.s.cs & 0xFFFF
        self.base = (self.ds << 4) & 0xFFFFF
        self.cbase = (self.cs << 4) & 0xFFFFF
        #: the swappable DGROUP backend (the NativeGameState seam), so the object walker's slot access follows
        #: a hybrid store like the contract passes; None over a raw VM cpu -> straight to .data.
        be = getattr(cpu.mem, "backend", None)
        self.backend = be if getattr(be, "_IS_DGROUP_BACKEND", False) else None
        self.map_seg = self.rw(LEVEL_DATA_SEG)
        self.tables = Tables(self.rb)          # the named read-only lookup tables

    def rb(self, off):
        if self.backend is not None:
            return self.backend.rb(off)
        return self.data[(self.base + (off & 0xFFFF)) & 0xFFFFF]

    def rw(self, off):
        if self.backend is not None:
            return self.backend.rw(off)
        b = self.base
        return self.data[(b + (off & 0xFFFF)) & 0xFFFFF] | (self.data[(b + ((off + 1) & 0xFFFF)) & 0xFFFFF] << 8)

    def wb(self, off, v):
        if self.backend is not None:
            self.backend.wb(off, v); return
        self.data[(self.base + (off & 0xFFFF)) & 0xFFFFF] = v & 0xFF

    def ww(self, off, v):
        if self.backend is not None:
            self.backend.ww(off, v); return
        b = self.base
        self.data[(b + (off & 0xFFFF)) & 0xFFFFF] = v & 0xFF
        self.data[(b + ((off + 1) & 0xFFFF)) & 0xFFFFF] = (v >> 8) & 0xFF

    def read_map(self, idx):
        return self.data[(((self.map_seg << 4) & 0xFFFFF) + (idx & 0xFFFF)) & 0xFFFFF]

    def prop_a(self, t):
        return self.tables.ceil_props[t]

    def prop_b(self, t):
        return self.tables.floor_props[t]

    def slope(self, t):
        return self.tables.tile_props[t]

    def cos_table(self, a):
        return self.tables.cos[a]

    def sin_table(self, a):
        return self.tables.sin[a]

    def tile_prop(self, tx, ty):
        return self.tables.floor_props[self.read_map((ty * 0x100 + tx) & 0xFFFF)]

    def scale(self):
        return self.rw(COMBO_COMPLETE_6BE2)

    def handler_addr(self, tbl):
        off = (tbl + _HANDLER_TABLE) & 0xFFFF
        return self.data[(self.cbase + off) & 0xFFFFF] | (self.data[(self.cbase + ((off + 1) & 0xFFFF)) & 0xFFFFF] << 8)

    def glb(self):
        return {"player_x": self.rw(PLAYER_SLOT), "player_y": self.rw(PLAYER_Y), "frame": self.rb(FRAME_TIMER),
                "shake": self.rb(SHAKE_MAGNITUDE), "a340": self.rb(ANIM_READY), "mode": self.rb(LEVEL_INDEX),
                "a30e": self.rw(QUAKE_DIST_LO), "a310": self.rw(QUAKE_DIST_HI), "bc0": self.rb(FIREFLY_SCRATCH_A), "bc1": self.rb(FIREFLY_SCRATCH_B),
                "bd0": self.rb(ANIM_GATE), "ror": self.rw(RNG_ROTATE), "la": self.rb(RNG_STATE), "lb": self.rb(RNG_STATE + 1),
                "lc": self.rb(RNG_STATE + 2), "ld": self.rw(RNG_STATE + 3)}

    def write_glb(self, g):
        self.ww(QUAKE_DIST_LO, g["a30e"]); self.ww(QUAKE_DIST_HI, g["a310"]); self.wb(FIREFLY_SCRATCH_A, g["bc0"])
        self.wb(FIREFLY_SCRATCH_B, g["bc1"]); self.ww(RNG_ROTATE, g["ror"]); self.wb(RNG_STATE, g["la"])
        self.wb(RNG_STATE + 1, g["lb"]); self.wb(RNG_STATE + 2, g["lc"]); self.ww(RNG_STATE + 3, g["ld"])

    def spawn(self, def9, defB, arg, dl):
        def find_free():
            di = _FX_LIST
            while self.rw(di) != 0xFFFF:
                di = (di + 6) & 0xFFFF
            return _Slot(self, di)
        spawn_effects(def9, defB, arg, dl, find_free)
