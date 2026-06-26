"""Bridge for the composed object-update walker (1030:684E..6913).

Presents the live VM memory as the :class:`~pre2.recovered.object_tick.object_tick` ``WalkerMem`` accessor —
in place (no copy): word/byte read+write in the object data segment, the level-map terrain lookups, the cos/
sin tables, the global-scale + shake/PRNG state, the AI handler-address table (``cs:[bx+0x6AA9]``), and the
effect-spawn emit (``spawn_effects`` over the ``0x7DE6`` list). Pure layout/translation — no game logic.
"""
from __future__ import annotations

from pre2.recovered.object_update import spawn_effects

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
        self.map_seg = self.rw(0x2DDA)

    def rb(self, off):
        return self.data[(self.base + (off & 0xFFFF)) & 0xFFFFF]

    def rw(self, off):
        b = self.base
        return self.data[(b + (off & 0xFFFF)) & 0xFFFFF] | (self.data[(b + ((off + 1) & 0xFFFF)) & 0xFFFFF] << 8)

    def wb(self, off, v):
        self.data[(self.base + (off & 0xFFFF)) & 0xFFFFF] = v & 0xFF

    def ww(self, off, v):
        b = self.base
        self.data[(b + (off & 0xFFFF)) & 0xFFFFF] = v & 0xFF
        self.data[(b + ((off + 1) & 0xFFFF)) & 0xFFFFF] = (v >> 8) & 0xFF

    def read_map(self, idx):
        return self.data[(((self.map_seg << 4) & 0xFFFFF) + (idx & 0xFFFF)) & 0xFFFFF]

    def prop_a(self, t):
        return self.rb(0x7E5E + t)

    def prop_b(self, t):
        return self.rb(0x7F5E + t)

    def slope(self, t):
        return self.rb(0x8E1D + t)

    def cos_table(self, a):
        return self.rb((0x6F90 + a) & 0xFFFF)

    def sin_table(self, a):
        return self.rb((0x7090 + a) & 0xFFFF)

    def tile_prop(self, tx, ty):
        return self.rb(0x7F5E + self.read_map((ty * 0x100 + tx) & 0xFFFF))

    def scale(self):
        return self.rw(0x6BE2)

    def handler_addr(self, tbl):
        off = (tbl + _HANDLER_TABLE) & 0xFFFF
        return self.data[(self.cbase + off) & 0xFFFFF] | (self.data[(self.cbase + ((off + 1) & 0xFFFF)) & 0xFFFFF] << 8)

    def glb(self):
        return {"player_x": self.rw(0x4F1C), "player_y": self.rw(0x4F1E), "frame": self.rb(0x6BD5),
                "shake": self.rb(0x6BEA), "a340": self.rb(0xA340), "mode": self.rb(0x2D8A),
                "a30e": self.rw(0xA30E), "a310": self.rw(0xA310), "bc0": self.rb(0x6BC0), "bc1": self.rb(0x6BC1),
                "bd0": self.rb(0x6BD0), "ror": self.rw(0x28C1), "la": self.rb(0x2CEC), "lb": self.rb(0x2CED),
                "lc": self.rb(0x2CEE), "ld": self.rw(0x2CEF)}

    def write_glb(self, g):
        self.ww(0xA30E, g["a30e"]); self.ww(0xA310, g["a310"]); self.wb(0x6BC0, g["bc0"])
        self.wb(0x6BC1, g["bc1"]); self.ww(0x28C1, g["ror"]); self.wb(0x2CEC, g["la"])
        self.wb(0x2CED, g["lb"]); self.wb(0x2CEE, g["lc"]); self.ww(0x2CEF, g["ld"])

    def spawn(self, def9, defB, arg, dl):
        def find_free():
            di = _FX_LIST
            while self.rw(di) != 0xFFFF:
                di = (di + 6) & 0xFFFF
            return _Slot(self, di)
        spawn_effects(def9, defB, arg, dl, find_free)
