"""Byte-backed *typed views* over the DGROUP image — the optional layout bridge.

This is the adapter that binds human-named gameplay fields to their original DOS memory offsets. Recovered
logic operates on a *view* (``view.wind``, ``view.slots[i].x``, ``view.script.threshold``) and never sees an
offset; this module is the ONLY place the DGROUP layout for its island is written down.

A view holds a **backend** (the ports-and-adapters seam) and its fields address the backend in DGROUP OFFSETS:

* :class:`ByteBackend` — reads/writes straight through the 1 MB image (``NativeGameState.data`` or a VM
  ``mem.data``). Byte-exact verification stays a plain memcmp of that image against the ASM oracle.
* :class:`OverlayBackend` — a read-through overlay: reads fall through to a base reader, writes ACCUMULATE a
  ``{offset: value}`` contract WITHOUT mutating the base. This is the backend for the contract-returning
  islands (the whole-routine transforms that return a write set, e.g. terrain-entities / player-interaction).

Because both backends share one interface, the SAME view (and the same recovered logic) runs over either — and
a release could add a third, field-backed backend (plain Python attributes, no offsets) behind the same view
API with no change to the logic. That is what makes the offset map the *optional* half of the split.
"""
from __future__ import annotations

DGROUP_BASE = 0x1A0F << 4       # DS<<4 — the game data segment's linear base in the 1 MB image


# ---- backends -----------------------------------------------------------------------------------------------

class ByteBackend:
    """Reads/writes go straight to the 1 MB image at ``DGROUP_BASE + offset``."""

    __slots__ = ("data",)

    def __init__(self, source):
        self.data = source.data if hasattr(source, "data") else source

    def rb(self, off: int) -> int:
        return self.data[DGROUP_BASE + (off & 0xFFFF)]

    def wb(self, off: int, v: int) -> None:
        self.data[DGROUP_BASE + (off & 0xFFFF)] = v & 0xFF

    def rw(self, off: int) -> int:
        a = DGROUP_BASE + (off & 0xFFFF)
        return self.data[a] | (self.data[a + 1] << 8)

    def ww(self, off: int, v: int) -> None:
        a = DGROUP_BASE + (off & 0xFFFF)
        self.data[a] = v & 0xFF
        self.data[a + 1] = (v >> 8) & 0xFF


class OverlayBackend:
    """Read-through overlay: reads fall through to ``base_rb(offset)`` unless already written; writes accumulate
    the ``writes`` contract (``{offset: byte}``) and never touch the base. A contract-returning island runs its
    whole-routine transform over one of these and returns ``overlay.writes`` as its write set — so the pass stays
    a pure function of its inputs (the base is untouched), exactly like the hand-written ``_Ov`` it replaces."""

    __slots__ = ("_base_rb", "writes")

    def __init__(self, base_rb):
        self._base_rb = base_rb          # base_rb(offset) -> the ORIGINAL DS byte at a DGROUP offset
        self.writes: dict[int, int] = {}

    def rb(self, off: int) -> int:
        o = off & 0xFFFF
        return self.writes[o] if o in self.writes else self._base_rb(o)

    def wb(self, off: int, v: int) -> None:
        self.writes[off & 0xFFFF] = v & 0xFF

    def rw(self, off: int) -> int:
        return self.rb(off) | (self.rb((off + 1) & 0xFFFF) << 8)

    def ww(self, off: int, v: int) -> None:
        self.wb(off, v)
        self.wb((off + 1) & 0xFFFF, v >> 8)


# ---- field descriptors (offset RELATIVE to the view's base) -------------------------------------------------

class _U16:
    """A little-endian 16-bit field."""

    def __init__(self, off: int):
        self.off = off

    def __get__(self, o, owner=None):
        if o is None:
            return self
        return o._backend.rw(o._base + self.off)

    def __set__(self, o, v: int):
        o._backend.ww(o._base + self.off, v)


class _U8:
    """An 8-bit field."""

    def __init__(self, off: int):
        self.off = off

    def __get__(self, o, owner=None):
        if o is None:
            return self
        return o._backend.rb(o._base + self.off)

    def __set__(self, o, v: int):
        o._backend.wb(o._base + self.off, v)


class _S16:
    """A little-endian *signed* 16-bit field (returns -0x8000..0x7FFF)."""

    def __init__(self, off: int):
        self.off = off

    def __get__(self, o, owner=None):
        if o is None:
            return self
        v = o._backend.rw(o._base + self.off)
        return v - 0x10000 if v & 0x8000 else v

    def __set__(self, o, v: int):
        o._backend.ww(o._base + self.off, v)


class _S8:
    """An 8-bit *signed* field (returns -0x80..0x7F)."""

    def __init__(self, off: int):
        self.off = off

    def __get__(self, o, owner=None):
        if o is None:
            return self
        v = o._backend.rb(o._base + self.off)
        return v - 0x100 if v & 0x80 else v

    def __set__(self, o, v: int):
        o._backend.wb(o._base + self.off, v)


class _U16Array:
    """A contiguous array of little-endian 16-bit words; ``view.field[i]`` reads/writes element ``i``."""

    def __init__(self, off: int, length: int):
        self.off = off
        self.length = length

    def __get__(self, o, owner=None):
        if o is None:
            return self
        return _U16ArrayView(o._backend, o._base + self.off, self.length)


class _U16ArrayView:
    __slots__ = ("_backend", "_base", "length")

    def __init__(self, backend, base: int, length: int):
        self._backend = backend
        self._base = base
        self.length = length

    def __getitem__(self, i: int) -> int:
        return self._backend.rw(self._base + i * 2)

    def __setitem__(self, i: int, v: int) -> None:
        self._backend.ww(self._base + i * 2, v)

    def __len__(self) -> int:
        return self.length


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
        return _StructArrayView(o._backend, o._base + self.off, self.stride, self.length, self.struct_cls)


class _StructArrayView:
    __slots__ = ("_backend", "_base", "_stride", "length", "_cls")

    def __init__(self, backend, base: int, stride: int, length: int, cls):
        self._backend = backend
        self._base = base
        self._stride = stride
        self.length = length
        self._cls = cls

    def __getitem__(self, i: int):
        if i < 0:
            i += self.length
        return self._cls(self._backend, self._base + i * self._stride)

    def __len__(self) -> int:
        return self.length

    def __iter__(self):
        for i in range(self.length):
            yield self._cls(self._backend, self._base + i * self._stride)


# ---- view bases ---------------------------------------------------------------------------------------------

class StructView:
    """A view over ONE fixed-layout struct at a DGROUP ``base`` offset; its field descriptors add their own
    (relative) offset to ``base``. Bind it to a backend + base — arrays hand it both."""

    __slots__ = ("_backend", "_base")

    def __init__(self, backend, base: int = 0):
        self._backend = backend
        self._base = base


class DgroupView(StructView):
    """A whole-DGROUP view (base 0, so field offsets ARE DGROUP offsets). Construct from a backend, or directly
    from a ``NativeGameState`` / VM ``mem`` / raw ``bytearray`` (wrapped in a :class:`ByteBackend`)."""

    __slots__ = ()

    def __init__(self, source):
        backend = source if isinstance(source, (ByteBackend, OverlayBackend)) else ByteBackend(source)
        super().__init__(backend, 0)


class _ScriptEntry(StructView):
    """A read-only cursor over one 6-byte scripted-scroll entry. Snapshots its base at construction, so it keeps
    reading the ORIGINAL entry even after the view advances ``script_ptr`` (matches the ASM ``bx`` loaded once)."""

    __slots__ = ()

    threshold      = _U16(0)
    delta          = _U16(2)
    clamp          = _U16(4)
    next_threshold = _U16(6)


# ---- island layouts (the offsets live here, nowhere else) ---------------------------------------------------

class ScrollScriptView(DgroupView):
    """The scripted-camera-scroll / LEVELG-snow state (1030:3922) as human-named fields."""

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
        return _ScriptEntry(self._backend, self.script_ptr)


# ---- array-of-structs: the firefly swarm (1030:54AB, the 0x6EA9 slot array) ---------------------------------

_DEAD_SLOT = 0x55AA         # a slot whose first word is this sentinel is dead


class FireflySlot(StructView):
    """One firefly: signed world position + a flicker timer. Stride-8 slot in the 0x6EA9 array."""

    __slots__ = ()

    x     = _S16(0)         # 0x55AA (raw) in this word marks a DEAD slot (see ``alive``)
    y     = _S16(2)
    timer = _U8(6)

    @property
    def alive(self) -> bool:
        return self._backend.rw(self._base) != _DEAD_SLOT


class SwarmView(DgroupView):
    """The 20-slot firefly swarm + the camera/page the draw reads (1030:54AB)."""

    __slots__ = ()

    slots   = StructArray(0x6EA9, 8, 20, FireflySlot)
    cam_col = _S16(0x2DE4)
    cam_row = _S16(0x2DE6)
    page    = _U16(0x2DD8)


# ---- the shared render/object slot (stride 0x12) ------------------------------------------------------------

class RenderSlot(StructView):
    """One on-screen entity record — the stride-0x12 slot the renderer + ride-collision read. It is the
    projection target of terrain-entities (0x5570), the object-render array, and the second pass; the same
    layout, so one view serves them all. ``sprite`` [+4] is a PACKED word: low 0x1FFF = sprite id, high bits =
    flags (0x2000 collectible, 0x4000 opaque flash); the ``flags`` byte aliases its high byte. A slot whose
    ``sprite`` is 0xFFFF is the dead/terminator entry."""

    __slots__ = ()

    x      = _U16(0)
    y      = _U16(2)
    sprite = _U16(4)       # packed: (id & 0x1FFF) | flag bits
    flags  = _U8(5)        # aliases the high byte of `sprite`
    source = _U16(9)       # back-ref to the source entity slot this was projected from

    @property
    def sprite_id(self) -> int:
        """The bare sprite index (``sprite`` with the flag bits masked off)."""
        return self._backend.rw(self._base + 4) & 0x1FFF
