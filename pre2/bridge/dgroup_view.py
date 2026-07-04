"""Byte-backed *typed views* over the DGROUP image — the optional layout bridge.

This is the adapter that binds human-named gameplay fields to their original DOS memory offsets. Recovered
logic operates on a *view* (``view.wind``, ``view.flakes[i]``, ``view.script.threshold``) and never sees an
offset; this module is the ONLY place the DGROUP layout for its island is written down. It is byte-backed —
the view reads and writes straight through to the 1 MB image (``NativeGameState.data`` or a VM ``mem.data``),
so byte-exact verification stays a plain memcmp of that image against the ASM oracle. It is the *optional*
half of the split: a release could swap in a field-backed adapter (plain Python attributes, no offsets)
behind the same view API without touching a line of the recovered logic.

Pilot scope: the scripted-scroll / LEVELG-snow island (1030:3922). Other islands migrate onto the same
descriptors (`_U8`/`_U16`/`_U16Array`) one at a time.
"""
from __future__ import annotations

DGROUP_BASE = 0x1A0F << 4       # DS<<4 — the game data segment's linear base in the 1 MB image


class _U16:
    """A little-endian 16-bit field at a fixed DGROUP offset."""

    def __init__(self, off: int):
        self.off = off

    def __get__(self, o, owner=None):
        if o is None:
            return self
        a = o._base + self.off
        d = o._data
        return d[a] | (d[a + 1] << 8)

    def __set__(self, o, v: int):
        a = o._base + self.off
        d = o._data
        d[a] = v & 0xFF
        d[a + 1] = (v >> 8) & 0xFF


class _U8:
    """An 8-bit field at a fixed DGROUP offset."""

    def __init__(self, off: int):
        self.off = off

    def __get__(self, o, owner=None):
        if o is None:
            return self
        return o._data[o._base + self.off]

    def __set__(self, o, v: int):
        o._data[o._base + self.off] = v & 0xFF


class _S16:
    """A little-endian *signed* 16-bit field at a fixed offset (returns a Python int in -0x8000..0x7FFF)."""

    def __init__(self, off: int):
        self.off = off

    def __get__(self, o, owner=None):
        if o is None:
            return self
        a = o._base + self.off
        d = o._data
        v = d[a] | (d[a + 1] << 8)
        return v - 0x10000 if v & 0x8000 else v

    def __set__(self, o, v: int):
        a = o._base + self.off
        d = o._data
        d[a] = v & 0xFF
        d[a + 1] = (v >> 8) & 0xFF


class _U16Array:
    """A contiguous array of little-endian 16-bit words; ``view.field[i]`` reads/writes element ``i``."""

    def __init__(self, off: int, length: int):
        self.off = off
        self.length = length

    def __get__(self, o, owner=None):
        if o is None:
            return self
        return _U16ArrayView(o._data, o._base + self.off, self.length)


class _U16ArrayView:
    __slots__ = ("_data", "_base", "length")

    def __init__(self, data, base: int, length: int):
        self._data = data
        self._base = base
        self.length = length

    def __getitem__(self, i: int) -> int:
        a = self._base + i * 2
        d = self._data
        return d[a] | (d[a + 1] << 8)

    def __setitem__(self, i: int, v: int) -> None:
        a = self._base + i * 2
        d = self._data
        d[a] = v & 0xFF
        d[a + 1] = (v >> 8) & 0xFF

    def __len__(self) -> int:
        return self.length


class DgroupView:
    """Base for a byte-backed struct view. Bind it to a ``NativeGameState`` (or any object exposing ``.data``,
    e.g. a VM ``mem``, so the same view verifies against the oracle) or a raw 1 MB ``bytearray``."""

    __slots__ = ("_data", "_base")

    def __init__(self, state):
        self._data = state.data if hasattr(state, "data") else state
        self._base = DGROUP_BASE


class StructView:
    """A view over ONE fixed-layout struct at an absolute linear base. Field descriptors (``_U8``/``_U16``/
    ``_S16`` with offsets RELATIVE to the struct) resolve against this base — so the same descriptors serve
    both whole-DGROUP views and array-of-structs elements."""

    __slots__ = ("_data", "_base")

    def __init__(self, data, base: int):
        self._data = data
        self._base = base


class StructArray:
    """A descriptor for a fixed-stride array of structs; ``view.field[i]`` returns ``struct_cls`` bound to
    ``base + i*stride`` (negative ``i`` wraps). Iterable and ``len()``-able."""

    def __init__(self, off: int, stride: int, length: int, struct_cls):
        self.off = off
        self.stride = stride
        self.length = length
        self.struct_cls = struct_cls

    def __get__(self, o, owner=None):
        if o is None:
            return self
        return _StructArrayView(o._data, o._base + self.off, self.stride, self.length, self.struct_cls)


class _StructArrayView:
    __slots__ = ("_data", "_base", "_stride", "length", "_cls")

    def __init__(self, data, base: int, stride: int, length: int, cls):
        self._data = data
        self._base = base
        self._stride = stride
        self.length = length
        self._cls = cls

    def __getitem__(self, i: int):
        if i < 0:
            i += self.length
        return self._cls(self._data, self._base + i * self._stride)

    def __len__(self) -> int:
        return self.length

    def __iter__(self):
        for i in range(self.length):
            yield self._cls(self._data, self._base + i * self._stride)


class _ScriptEntry:
    """A read-only cursor over one 6-byte scripted-scroll entry ``{threshold, delta, clamp, next_threshold}``.
    Snapshots its base at construction, so it keeps reading the ORIGINAL entry even after the view advances
    ``script_ptr`` (matches the ASM, which loads ``bx`` once at 3930)."""

    __slots__ = ("_data", "_base")

    def __init__(self, data, entry_base: int):
        self._data = data
        self._base = entry_base

    def _w(self, o: int) -> int:
        a = self._base + o
        return self._data[a] | (self._data[a + 1] << 8)

    @property
    def threshold(self) -> int:
        return self._w(0)

    @property
    def delta(self) -> int:
        return self._w(2)

    @property
    def clamp(self) -> int:
        return self._w(4)

    @property
    def next_threshold(self) -> int:
        return self._w(6)


class ScrollScriptView(DgroupView):
    """The scripted-camera-scroll / LEVELG-snow state (1030:3922) as human-named fields.

    Layout bridge for ``pre2.recovered.scroll_script`` — the one place this island's DGROUP offsets live.
    """

    __slots__ = ()

    frame_counter = _U16(0x2DBE)   # the scripted-scroll frame counter [asm 3922]
    script_ptr    = _U16(0x2DBC)   # pointer to the current 6-byte script entry [asm 3930]
    wind          = _U16(0x6BF6)   # accumulated WIND (flake count + player push + snow render) [asm 3940]
    tick          = _U8(0x6BD5)    # free-running frame counter (the &3 4-frame gate) [asm 3926]
    camera_x      = _U16(0x2DE4)   # horizontal camera the flake position is taken relative to [asm 3998]
    draw_page     = _U16(0x2DD8)   # draw-page byte base (unused by state; the renderer adds its own page)
    # the four-byte snow/gameplay PRNG state [0x2CEC..0x2CF1] (rng_lcg operates on these)
    rng_a         = _U8(0x2CEC)
    rng_b         = _U8(0x2CED)
    rng_c         = _U8(0x2CEE)
    rng_d         = _U16(0x2CEF)
    flakes        = _U16Array(0x6CA9, 0x100)   # the 0x100-word flake-position array [asm 3988]

    @property
    def script(self) -> _ScriptEntry:
        """The current 6-byte script entry (read at ``script_ptr``)."""
        return _ScriptEntry(self._data, self._base + self.script_ptr)


# ---- array-of-structs pilot: the firefly swarm (1030:54AB, the 0x6EA9 slot array) ---------------------------

_DEAD_SLOT = 0x55AA         # a slot whose first word is this sentinel is dead


class FireflySlot(StructView):
    """One firefly: signed world position + a flicker timer. Stride-8 slot in the 0x6EA9 array."""

    __slots__ = ()

    x     = _S16(0)         # 0x55AA (raw) in this word marks a DEAD slot (see ``alive``)
    y     = _S16(2)
    timer = _U8(6)

    @property
    def alive(self) -> bool:
        a = self._base
        return (self._data[a] | (self._data[a + 1] << 8)) != _DEAD_SLOT


class SwarmView(DgroupView):
    """The 20-slot firefly swarm + the camera/page the draw reads (1030:54AB)."""

    __slots__ = ()

    slots   = StructArray(0x6EA9, 8, 20, FireflySlot)
    cam_col = _S16(0x2DE4)
    cam_row = _S16(0x2DE6)
    page    = _U16(0x2DD8)
