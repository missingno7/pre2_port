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
from pre2.native.dgroup_offsets import (
    PALETTE_PTR_TABLE, RENDER_SLOTS_BASE)

DGROUP_BASE = 0x1A0F << 4       # DS<<4 — the game data segment's linear base in the 1 MB image


# ---- backends -----------------------------------------------------------------------------------------------

class ByteBackend:
    """Reads/writes go straight to the 1 MB image at ``DGROUP_BASE + offset``."""

    _IS_DGROUP_BACKEND = True
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


class SegmentBackend:
    """Reads/writes through the 1 MB image at ``(seg << 4) + (offset & 0xFFFF)`` — a typed-view backend for
    the game's OTHER segments (the ``[0x2DDA]`` level map, the ``[0x2875]`` asset/trigger bank). Offsets wrap
    at 64 KB exactly like the 16-bit registers the ASM addresses them with. The same :class:`StructView`
    machinery runs over it unchanged — only the base translation differs from :class:`ByteBackend`."""

    __slots__ = ("data", "base")

    def __init__(self, source, seg: int):
        self.data = source.data if hasattr(source, "data") else source
        self.base = (seg & 0xFFFF) << 4

    def rb(self, off: int) -> int:
        return self.data[(self.base + (off & 0xFFFF)) & 0xFFFFF]

    def wb(self, off: int, v: int) -> None:
        self.data[(self.base + (off & 0xFFFF)) & 0xFFFFF] = v & 0xFF

    def rw(self, off: int) -> int:
        return self.rb(off) | (self.rb(off + 1) << 8)

    def ww(self, off: int, v: int) -> None:
        self.wb(off, v)
        self.wb(off + 1, v >> 8)


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

    def apply(self, writes: dict) -> None:
        """Merge a ``{offset: (value, width)}`` width-tracking contract (e.g. another routine's return value)
        into this byte-level overlay."""
        for off, (val, width) in writes.items():
            (self.ww if width == 2 else self.wb)(off, val)

    def merge_bytes(self, byte_writes: dict) -> None:
        """Merge another byte-level ``{offset: value}`` dict (e.g. a nested overlay's ``writes``) into this one."""
        for off, val in byte_writes.items():
            self.writes[off & 0xFFFF] = val & 0xFF


class WidthOverlayBackend:
    """Read-through overlay emitting the ``{offset: (value, width)}`` width-tracking contract (vs
    :class:`OverlayBackend`'s byte-level ``{offset: value}``): reads fall through to ``base_rb(offset)`` unless
    already written; writes accumulate into ``writes`` (re-inserted on overwrite so last-write wins) and are
    visible to LATER reads in the same pass — the composition seam several islands need when one sub-call's
    write must be visible to the next (e.g. object_spawn's camera-boundary crush, whose targets read the
    previous bounce's velocity write). String keys in a write dict pass through unchanged (a sentinel, e.g.
    object_spawn's SONG_REQUEST) rather than being byte-decomposed."""

    _IS_DGROUP_BACKEND = True   # a dgroup-view backend: any StructView binds straight onto it

    __slots__ = ("_rb", "_bytes", "writes")

    def __init__(self, base_rb, base_rw=None):
        self._rb = base_rb         # base_rb(offset) -> the ORIGINAL DS byte at a DGROUP offset (base_rw unused:
        self._bytes: dict[int, int] = {}   # words are always composed from two byte reads, like real memory)
        self.writes: dict = {}

    def rb(self, o: int) -> int:
        o &= 0xFFFF
        return self._bytes.get(o, self._rb(o)) & 0xFF

    def rw(self, o: int) -> int:
        o &= 0xFFFF
        return self.rb(o) | (self.rb((o + 1) & 0xFFFF) << 8)

    def apply(self, writes: dict) -> None:
        for off, v in writes.items():
            if isinstance(off, str):                     # sentinel — pass through to the contract untouched
                self.writes[off] = v
                continue
            val, wd = v
            off &= 0xFFFF
            self.writes.pop(off, None)
            self.writes[off] = (val, wd)
            self._bytes[off] = val & 0xFF
            if wd == 2:
                self._bytes[(off + 1) & 0xFFFF] = (val >> 8) & 0xFF

    def wb(self, o: int, v: int) -> None:
        self.apply({o & 0xFFFF: (v & 0xFF, 1)})

    def ww(self, o: int, v: int) -> None:
        self.apply({o & 0xFFFF: (v & 0xFFFF, 2)})


class WidthContractBackend:
    """A write-only contract accumulator emitting ``{offset: (value, width)}`` — the width-tracking contract
    convention some islands use (vs :class:`OverlayBackend`'s byte-level ``{offset: value}``). Reads delegate to
    the island's own ``rb``/``rw`` closures (which may be word-granular, not byte-composed) and do NOT see the
    accumulated writes — for projection passes that read only original memory and emit a fresh write set."""

    __slots__ = ("_rb", "_rw", "writes")

    def __init__(self, base_rb, base_rw, out: "dict[int, tuple[int, int]] | None" = None):
        self._rb = base_rb
        self._rw = base_rw
        self.writes: dict[int, tuple[int, int]] = {} if out is None else out

    def rb(self, off: int) -> int:
        return self._rb(off & 0xFFFF)

    def rw(self, off: int) -> int:
        return self._rw(off & 0xFFFF)

    def wb(self, off: int, v: int) -> None:
        self.writes[off & 0xFFFF] = (v & 0xFF, 1)

    def ww(self, off: int, v: int) -> None:
        self.writes[off & 0xFFFF] = (v & 0xFFFF, 2)


class DictBackend:
    """The PLAIN ``{offset: value}`` write-contract accumulator (vs :class:`WidthContractBackend`'s
    ``(value, width)`` tuples) — the convention the player-collision island returns, where the width is
    implicit (the consumer splits words vs its byte-field set). Reads delegate to the island's ``rb``/``rw``
    closures and do NOT see the accumulated writes (functions that need read-after-write keep a local, exactly
    like the hand-built dicts they replace). A named view bound to one of these makes ``p.yvel = 0`` record
    ``{0x4F2A: 0}`` — same contract, no offset at the call site."""

    __slots__ = ("_rb", "_rw", "writes")

    def __init__(self, base_rb, base_rw, out: dict | None = None):
        self._rb = base_rb
        self._rw = base_rw
        self.writes: dict[int, int] = {} if out is None else out

    def rb(self, off: int) -> int:
        return self._rb(off & 0xFFFF)

    def rw(self, off: int) -> int:
        return self._rw(off & 0xFFFF)

    def wb(self, off: int, v: int) -> None:
        self.writes[off & 0xFFFF] = v & 0xFF

    def ww(self, off: int, v: int) -> None:
        self.writes[off & 0xFFFF] = v & 0xFFFF


def apply_contract(state, writes, *, word_fields=None) -> None:
    """THE single seam every island write-contract crosses to reach live state.

    Applies a recovered write-contract to the game state through a :class:`ByteBackend`. The three contract
    conventions are handled uniformly:

    * ``{offset: (value, width)}`` — a tuple value is self-describing (the width-contract islands);
    * ``{offset: value}`` with ``word_fields`` — a plain value is a WORD iff its offset is in the set
      (the FSM convention, ``FSM_WORD_FIELDS``);
    * ``{offset: value}`` without ``word_fields`` — plain values are BYTES (the byte-level overlay
      contracts: collision, terrain).

    Sentinel keys (strings like SONG_REQUEST/SCROLL_REQUEST) must be popped by the caller BEFORE applying —
    a string key here is a bug and raises.

    THE FLIP POINT: when the product's state becomes field-backed, this function (plus the read half,
    the backends) is where the offset world ends — the contract application resolves through the generated
    field registry instead of a byte image, and this module's offset map moves to the detachable bridge.

    Routes through ``state.backend`` when present (the NativeGameState seam — swappable to a hybrid store),
    else a fresh :class:`ByteBackend` (the workbench's VM state / raw images)."""
    be = getattr(state, "backend", None)
    if be is None or not getattr(be, "_IS_DGROUP_BACKEND", False):
        be = ByteBackend(state)
    for off, v in writes.items():
        if isinstance(v, tuple):
            val, width = v
        else:
            val, width = v, (2 if word_fields is not None and off in word_fields else 1)
        if width == 2:
            be.ww(off, val)
        else:
            be.wb(off, val)


# ---- field descriptors (offset RELATIVE to the view's base) -------------------------------------------------
#
# Each descriptor also captures its own attribute NAME (``__set_name__``) so a view can be resolved by NAME as
# well as by offset. When the bound backend exposes ``read_field`` / ``write_field`` (the shipped, offset-free
# NamedObjectBackend — pre2/views/named_view.py, the native-dataclass lift), access dispatches by name to the
# ``pre2/game`` dataclass; otherwise it uses the offset exactly as before (the shipped ByteBackend path is
# byte-for-byte unchanged). The offset stays here through the P2/P3 transition and is removed at P4, when it
# moves wholly into the detachable bridge layout.

def _read_field(o, off: int, name: str, width: int, signed: bool) -> int:
    be = o._backend
    rf = getattr(be, "read_field", None)
    if rf is not None:
        return rf(o, name, width, signed)
    v = be.rb(o._base + off) if width == 1 else be.rw(o._base + off)
    if signed and v & (1 << (8 * width - 1)):
        v -= 1 << (8 * width)
    return v


def _write_field(o, off: int, name: str, width: int, v: int) -> None:
    be = o._backend
    wf = getattr(be, "write_field", None)
    if wf is not None:
        wf(o, name, width, v)
    elif width == 1:
        be.wb(o._base + off, v)
    else:
        be.ww(o._base + off, v)


class _U16:
    """A little-endian 16-bit field."""

    def __init__(self, off: int):
        self.off = off
        self.name = ""

    def __set_name__(self, owner, name: str):
        self.name = name

    def __get__(self, o, owner=None):
        if o is None:
            return self
        return _read_field(o, self.off, self.name, 2, False)

    def __set__(self, o, v: int):
        _write_field(o, self.off, self.name, 2, v)


class _U8:
    """An 8-bit field."""

    def __init__(self, off: int):
        self.off = off
        self.name = ""

    def __set_name__(self, owner, name: str):
        self.name = name

    def __get__(self, o, owner=None):
        if o is None:
            return self
        return _read_field(o, self.off, self.name, 1, False)

    def __set__(self, o, v: int):
        _write_field(o, self.off, self.name, 1, v)


class _S16:
    """A little-endian *signed* 16-bit field (returns -0x8000..0x7FFF)."""

    def __init__(self, off: int):
        self.off = off
        self.name = ""

    def __set_name__(self, owner, name: str):
        self.name = name

    def __get__(self, o, owner=None):
        if o is None:
            return self
        return _read_field(o, self.off, self.name, 2, True)

    def __set__(self, o, v: int):
        _write_field(o, self.off, self.name, 2, v)


class _S8:
    """An 8-bit *signed* field (returns -0x80..0x7F)."""

    def __init__(self, off: int):
        self.off = off
        self.name = ""

    def __set_name__(self, owner, name: str):
        self.name = name

    def __get__(self, o, owner=None):
        if o is None:
            return self
        return _read_field(o, self.off, self.name, 1, True)

    def __set__(self, o, v: int):
        _write_field(o, self.off, self.name, 1, v)


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


class _U8Array:
    """A contiguous array of bytes; ``view.field[i]`` reads/writes element ``i``."""

    def __init__(self, off: int, length: int):
        self.off = off
        self.length = length

    def __get__(self, o, owner=None):
        if o is None:
            return self
        return _U8ArrayView(o._backend, o._base + self.off, self.length)


class _U8ArrayView:
    __slots__ = ("_backend", "_base", "length")

    def __init__(self, backend, base: int, length: int):
        self._backend = backend
        self._base = base
        self.length = length

    def __getitem__(self, i):
        if isinstance(i, slice):
            return bytes(self._backend.rb(self._base + k) for k in range(*i.indices(self.length)))
        return self._backend.rb(self._base + i)

    def __setitem__(self, i, v) -> None:
        if isinstance(i, slice):
            for k, b in zip(range(*i.indices(self.length)), v):
                self._backend.wb(self._base + k, b)
            return
        self._backend.wb(self._base + i, v)

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

    @property
    def offset(self) -> int:
        """The struct's DGROUP base offset — TRANSITIONAL interop for the offset-keyed helpers a slot is
        passed to during the field-backed migration (e.g. ``hitbox_overlap(rb, rw, slot.offset, di)``).
        New code should pass the VIEW, not the offset; this property is the migration seam, not the API."""
        return self._base


def _coerce_backend(source):
    """A backend passes through — the package's own backends plus anything marked ``_IS_DGROUP_BACKEND``
    (island-local overlays like player_collision's read-through ``_Overlay`` opt in with that attribute; the
    VM ``mem`` object must NOT pass, its ``rb`` takes (seg, off)). A state object carrying its own swappable
    ``.backend`` (NativeGameState) binds to THAT — so a view follows the hybrid store, not a fresh image
    wrapper. Anything else (VM ``mem`` / raw ``bytearray``) is wrapped in a :class:`ByteBackend`."""
    if isinstance(source, (ByteBackend, SegmentBackend, OverlayBackend, WidthContractBackend, DictBackend)) \
            or getattr(source, "_IS_DGROUP_BACKEND", False) \
            or hasattr(source, "read_field"):            # a name-keyed backend (NamedObjectBackend) — pass through
        return source
    be = getattr(source, "backend", None)
    if be is not None and getattr(be, "_IS_DGROUP_BACKEND", False):
        return be
    return ByteBackend(source)


class DgroupView(StructView):
    """A whole-DGROUP view (base 0, so field offsets ARE DGROUP offsets). Construct from a backend, or directly
    from a ``NativeGameState`` / VM ``mem`` / raw ``bytearray`` (wrapped in a :class:`ByteBackend`)."""

    __slots__ = ()

    def __init__(self, source):
        super().__init__(_coerce_backend(source), 0)


class PlayerGlobals(DgroupView):
    """The player islands' DGROUP globals, NAMED — the readability contract for the collision routine
    (`1030:5A96`, pre2/recovered/player_collision.py) and the player FSM (`1030:58A7..`, pre2/recovered/
    player.py). Each field's offset + width + meaning is the recovered evidence (the [asm] site that
    reads/writes it); gameplay code reads ``g.airborne``, never ``rb(0x6BF3)`` — the offset lives HERE, once.
    Uncertain-purpose fields keep an honest ``unk_`` name until evidence firms."""

    __slots__ = ()

    airborne      = _U8(0x6BF3)   # bit0 = airborne [asm 6401]; 2 = grounded [64EE]; 0xFF = off-top [5B8E]
    fall_frames   = _U8(0x6BD2)   # the fall counter: ++ per descending airborne tick [5B4E]; classifies the
    #                               landing impact (soft <=4 / dust / shake >=0x14 / bounce >0xA) [647C..64D3]
    fall_latch    = _U8(0x6BD1)   # cleared on every landing [64E9/64DF]; the JUMP arc's frame counter [5F46]
    fall_grace    = _U8(0x6BE0)   # = 6 while falling [63DD]; saturating-dec on each soft land [64DF-64E4];
    #                               nonzero routes the jump handler into idle [5F37]
    last_land_y   = _U16(0x6BCA)  # Y of the last landing — the fall-height reference [64F3/6499]
    camera_shake  = _U8(0x6BEA)   # = 8 on a hard fall -> the camera-shake kick [64AE]; timer-decremented [5A4A+]
    low_gravity   = _U8(0x6BC7)   # == 1: lighter gravity (4) + terminal>>3 [6313]; cleared on land [642D];
    #                               the GLIDER descend flag (bit0 armed at speed [5A06], bit1 or'd [59CF])
    drop_gate     = _U8(0x6BE1)   # nonzero: ground-handler-5 tiles FALL THROUGH instead of landing [664A];
    #                               set 4 by the anim4/anim5 handlers [5E6C/5EA5]; timer-decremented
    input_lr      = _U8(0x6BDB)   # left|right held (the FSM front-end combines [0x27EC]|[0x27ED] [58A7]);
    #                               drives player_accel's input_held / the air drift [62B9]
    input_ud      = _U8(0x6BDC)   # up|down held ([0x27EA]|[0x27EB] [58B4])
    glider        = _U8(0x6BC5)   # the GLIDER/flying gate — armed by the glider pickup [484E/5960/63C3]
    trail_ring    = _U16(0x6BBE)  # the landing-dust / trail effect ring cursor (5E18's ring) [6483]
    anim_gate     = _U8(0x6BD0)   # nonzero: hold the current anim / route the FSM into the 5F93 override tail
    #                               [63E2/5F35]; the attack writes it from the anim high byte's ~bit6 [5FC3]
    current_hit_object = _U16(0x6BB1)  # cyxx current_hit_object: the FSM's current-object ptr; NULL on the player path [6698]
    dipping_tile  = _U16(0x6BAB)  # map offset of the currently-sagging bridge tile; 0x55AA = none [5BBB/5BF4]
    grid_dirty    = _U8(0x2DF4)   # whole-grid redraw request [5C82]
    grid_dirty_token = _U16(0x2DE0)  # its 0x55AA companion token [5C87]; a genuine union -- native_sync_
    #                               render_state also stamps this word with the render-mirror previous-camera
    #                               cell X (the pair to prev_cam_cell_y at 0x2DE2)
    page_dirty    = _U8(0x6BBD)   # one-tile direct re-blit page flag (the 653D path) [659C]
    cam_col       = _U8(0x2DE4)   # camera tile column [6546] (byte read — the on-screen test)
    cam_row       = _U8(0x2DE6)   # camera tile row [6551]
    cam_col_word  = _U16(0x2DE4)  # width alias: the camera-range check reads the same cells as WORDS [5ADF]
    cam_row_word  = _U16(0x2DE6)  # width alias of cam_row [5ACD]
    respawn_state = _U8(0x6BE4)   # 0 = none; 2 = armed by the off-camera death trigger [65B3/65C9]
    lives         = _U8(0x27D8)   # consumed by the off-camera trigger [65C1]; set 2 by main 0141
    energy        = _U8(0x27D6)   # hit-points within a life: the crush chain decrements it (5-frame cooldown
    #                               reload [824D]) and a life is consumed on underflow [825F]; reset 0 with the
    #                               life consume [65C5]. (object_spawn's old 'LIVES' constant misnamed this.)
    end_signal    = _U8(0x6BE5)   # 1 = game over (no lives) [65D0]; 0xFF = game complete (level 0xE) [5B1F];
    #                               doubles as DC1's demo-end sentinel flag (the ASM reuses the byte)
    map_rows      = _U8(0x2CF5)   # the map's bottom row bound [5B9D/5B0A]
    map_seg       = _U16(0x2DDA)  # the loaded level-data base segment (es for the tile grid) [asm 3f2b]
    display_page  = _U16(0x2DD6)  # the CRTC display-start PAGE the present flips [2DD6; read at every present]
    input_source  = _U8(0x2879)   # DC1's source: 0 live keyboard / 1 demo-attract playback / 2 record [0DC1]
    level_end_mode = _U8(0x6BE6)  # the 4C69 level-end dispatch mode: 1 normal end / >1 warp [4C69/4C74]
    checkpoint_x  = _U16(0x6BAD)  # the respawn/checkpoint player X the 4F6C respawn drops the player at [4fc2]
    checkpoint_y  = _U16(0x6BAF)  # ... checkpoint Y [4fcb]
    level         = _U8(0x2D8A)   # the current level index [5B18]; 0xFF = none chosen yet [8ee9]
    mode          = _U8(0xB197)   # 0 = BEGINNER / 1 = EXPERT — the mode-select toggle [9941/8ee9]
    mode_copy     = _U8(0xB198)   # the committed copy the loader reads [994E]
    attract_mode  = _U8(0x083D)   # the attract-demo header's mode byte (set with the commit) [994E]
    attract_level = _U8(0x083E)   # the attract/default level header [8E98]
    level_flags   = _U8(0x8166)   # bit0 = suppress the hard-land bounce [64BA]; bit1 = no idle camera-pan
    #                               [5D95]; bit2 = top-kill fence [5AF1]
    unk_6BFE      = _U8(0x6BFE)   # post-worker: nonzero -> the 64DF soft-land tail instead of air physics
    #                               [5B38]; zeroed by the jump body [5F41]; gates the attack Yvel nudge [6004]
    aura_toggle   = _U8(0x6BCC)   # cleared at level-init [asm 01f5]; also the idx0 aura-handler's alternating
    #                               +/-0xC0 side flag (PlayerState.aura_toggle) -- role beyond that not evidenced

    # --- the FSM's own state (pre2/recovered/player.py) ---
    idle_timer    = _U8(0x6BD3)   # sat-inc per run/attack frame [5EF9/5F9F]; >=0x1E = long idle [5D49];
    #                               zeroed by anim4 [5E6C]; -3 per long-idle frame [5D60]
    fly_timer     = _U8(0x6BC8)   # glider hold/flight counter [5EE4/5F13]; wing-anim bump gate [48DD]
    fly_hold      = _U8(0x6BC6)   # glider hold budget [5F1A=0x18, 599E dec, 59B8 sat-dec, 59F7 recharge]
    glider_tilt   = _U8(0x7B1A)   # glider tilt/pitch 0..6, neutral 3 [597E/5993/48A3]
    anim_hi       = _U8(0x6BCF)   # advance_anim's raw frame high byte [6398]; the attack reads ~bit6 [5FC3]
    run_count     = _U16(0x6BEB)  # inc-wrap-to-1 run counter [5952]; reset on a turn [58F4] / anim change [5947]
    input_suppress = _U8(0x6BCD)  # nonzero forces the input bitmask to 0 [5921]; the attack loads it from the
    #                               phase record's sfx byte [5FD2]; timer-decremented [5A4A+]
    charge        = _U8(0x6BCE)   # the +2-while-<=0x30 charge counter [5EB7]; quadruples attack v19 [5FB5]
    frame_blink   = _U8(0x6BD5)   # frame counter gating the trail emit to every 4th frame [5E11]
    frame_stamp   = _U16(0x6BD5)  # width alias: the debounce sites read the same counter as a WORD [80F7/8AB1]
    friction      = _U16(0x6BF6)  # the per-level directional-friction constant [62ED]
    cam_left      = _U16(0x8164)  # camera-left tile — the X-integrate right bound [5A1C]
    attack_phase  = _U8(0x7B18)   # index into the 5-byte attack phase records at 0x7B04 [5FA9]
    attack_v19    = _U8(0x7B19)   # loaded from phase.v19 (x4 when charged) [5FC0]; the fresh-start block
    #                               presets 0x14 [0141..] — exact consumer not yet mapped
    idle_clock    = _U16(0x27F0)  # the PIT-fed idle counter (the fidget selector reads &0x1FF [5DC9])
    unk_6BD9      = _U8(0x6BD9)   # nonzero suppresses the idle look-around camera pan [5D9B]

    # --- the effect-burst / boss-fight block (object_spawn + combat_interaction) ---
    burst_x       = _U16(0xA336)  # the spawn_effect_burst origin X (8D1B reads it) [8264/74A8/7041]
    burst_y       = _U16(0xA338)  # ... origin Y [826A/74B1]
    burst_sprite  = _U16(0xA33A)  # ... the burst sprite id [8285/9507/74E0]
    bonus_debounce = _U16(0xA33C)  # last-collect frame timestamp (vs frame_stamp, debounce window 6)
    #                                [combat_interaction bonus_hit_handler 8AB1]
    hit_flag      = _U8(0xA330)   # hitbox_overlap's vertical-detail hit flag (1 = registered) [8D7B]
    hit_pass_full = _U8(0xA312)   # set across a projectile/player pass -> 8D7B uses the FULL (un-halved)
    #                               tolerance [6FCF/6FD7]
    hit_debounce  = _U16(0xA3FD)  # frame stamp of the last camera-target hit (debounce window 0x16) [8109]
    boss_phase    = _U16(0xA326)  # the L6/boss fight phase (advances every 7 hits) [7036/7DA9]
    boss_x        = _U16(0x5648)  # the boss/camera-target record origin X [74A8/7041]
    boss_y        = _U16(0x564A)  # ... origin Y [74B1/704A]

    collected_counter = _U16(0x2A76)  # bumped once per bonus collected (the tally-percent numerator) [8B77]
    collected_linked = _U16(0x2A7A)  # the LINKED-item collected count (tally percent = [0x2A76]+this) [85CC/5139]

    # --- score + the camera/scroll engine scalars (the 0x6BC0..0x6C11 band) ---
    score_mid     = _U16(0x6C0C)  # a third score word, zeroed alongside score_lo/hi on game-over; role beyond
    #                               that not yet evidenced [asm 5083-508f]
    score_lo      = _U16(0x6C0E)  # the 32-bit score, low word [asm 5139/85B6]
    score_hi      = _U16(0x6C10)  # ... high word
    item_total    = _U16(0x6C9E)  # the collected-item total word [asm 51e9]
    scale_level   = _U16(0x6BE2)  # object_update's sprite scale level; doubles as player_interaction's
    #                               instadeath gate; timer-decremented (TIMER_WORD) [asm 5A82]
    hurt_cooldown = _U8(0x6BC9)   # the crush-damage cooldown (reload 5; a life on underflow) [asm 8254]
    bonus_flash   = _U8(0x6C00)   # the bonus-collect flash timer (render_state; timer-decremented) [5A7E]
    fine_scroll   = _U8(0x6BC4)   # sub-tile pixel scroll 0..15 (the frame engine) [frame VAR_FINE_SCROLL]
    scroll_copy_src = _U16(0x2DBA)  # the vertical-scroll VRAM copy-source offset (calc_scroll_source's result)
    #                               [camera_scroll _v_scroll_up/_down 33F8/3394 -> the 3588 tile-row redraw]
    col_ring      = _U16(0x2DE8)  # the background column ring index (camera_x % ring) [frame VAR_COL_RING]
    row_ring      = _U16(0x2DEA)  # the row-ring buffer index (camera_y % 0xC), written alongside col_ring
    #                               [camera_scroll _v_scroll_up/_down 33F5/338F]
    prev_cam_cell_y = _U16(0x2DE2)  # render-mirror previous-camera cell Y [native_sync_render_state]
    anim_remap_ptr = _U16(0x6BC2)   # the animated-tile remap frame pointer (367D steps it per redraw)
    anim_remap_thresh = _U8(0x6BD4)  # its threshold/counter byte
    scroll_anim_ctr = _U8(0x2DF5)  # sat-inc per vertical scroll step, drives the tile-row redraw animation
    #                               [camera_scroll _v_scroll_down/_up 33C4/3373]
    firefly_scratch_a = _U8(0x6BC0)  # the firefly pass's per-frame scratch pair [firefly_sim]
    firefly_scratch_b = _U8(0x6BC1)
    row_factor    = _U16(0x6BF8)  # the row-stride factor (0x28 * this) the shake writes [frame/camera_shake]
    entity_vx_scratch = _U16(0x6BFA)  # the terrain-entity system's per-slot X velocity scratch, read by the
    #                               4B05 player-ride collision to push the player [terrain_entities]
    entity_vy_scratch = _U16(0x6BFC)  # ... Y velocity scratch
    timer_6BE8    = _U8(0x6BE8)   # one of the 5A47 countdown timers (TIMER_BYTES); consumer not yet mapped
    cam_scroll_idle = _U8(0x6BEE)  # the vertical scroll-follow active flag (nonzero = scrolling toward
    #                               scroll_target_row) [camera_scroll _v_follow/_v_scroll_apply 56D9/5703]
    scroll_dir    = _U8(0x6BED)   # the horizontal scroll-follow direction state (0 idle/1 right/2 left)
    #                               [camera_scroll _h_follow/_h_init 57CC/5813] -- BYTE-width: 0x6BED is one
    #                               byte, immediately followed by the unrelated cam_scroll_idle byte at 0x6BEE;
    #                               level_init's [asm 3b38] "both axes idle" check reads them together as one
    #                               word == 0, reproduced explicitly as scroll_dir==0 and cam_scroll_idle==0.
    scroll_target_row = _U16(0x6BF1)  # the vertical scroll-follow's target row [camera_scroll _v_follow 56D9]
    scroll_phase  = _U8(0x6C05)   # the camera/scroll phase counter (757A sat-inc) [asm 757A/8131]
    scroll_vx     = _U16(0x6C06)  # the scroll-cursor X velocity [asm 70FE/815E]
    scroll_vy     = _U16(0x6C08)  # ... Y velocity (gravity 0x10, cap 0xE0) [asm 7118/8144]
    script_last   = _U16(0x6C0A)  # camera-script pointer last seen; a change resets script_cursor [asm 7537]
    level_prop_header = _U16(0x815E)  # the level's top-row property header [camera_scroll _v_scroll_apply
    #                               56EA, indexes LEVEL_PROP table -> the level-top camera clamp]
    scroll_speed_curve_ptr = _U16(0x78C4)  # an alternate scroll-speed curve base (grid_dirty selects it over
    #                               the fixed 0x78C6 table) [camera_scroll _v_speed 5750/578A]

    bonus_letters = _U8(0x6CA7)   # the collected BONUS-letters bitmask (0x1F = all five -> reward) [67D7]
    utensils_mask = _U8(0x6CA8)   # the collected-utensils bitmask (fresh start zeroes it) [asm 0155]
    quake_dist_lo = _U16(0xA30E)  # the boss-quake player-distance^2 scratch, low word [object_update 724]
    quake_dist_hi = _U16(0xA310)  # ... high word (must be 0 for the quake proximity test)
    sprite_bank_lo = _U16(0x8C89)  # entity sprite-ref bank base A (level_load 4182 rebases against it)
    sprite_bank_hi = _U16(0x8C8B)  # ... bank base B (reset to 0x35/0x138 after the rebase)
    # --- the spawner / camera-sequencer scalars (object_spawn + combat_interaction's globals) ---
    hit_detail    = _U16(0xA331)  # vertical penetration depth when hit_flag set [asm 8D7B]
    spawned_ptr   = _U16(0xA33E)  # the just-spawned burst-slot pointer (8C72 reads it back) [asm 88B9]
    anim_ready    = _U8(0xA340)   # object_update's per-step anim-ready scratch byte
    spawn_offset_ring = _U16(0xA341)  # 16-slot ring index into the spawn X-offset table [object_inject]
    proj_slot_ptr = _U16(0xA32E)  # the last object-list slot the 2nd-pass walker projected into [object_inject]
    spawn_count   = _U16(0x91FC)  # the level spawn param (>>3 = spawn count; boss damage decrements)
    cam_state     = _U8(0x91FE)   # the camera sequencer's 8-state machine state (0xFF = disabled)
    cursor_x      = _U16(0x91FF)  # the spawn/camera cursor position
    cursor_y      = _U16(0x9201)
    cam_timer     = _U16(0xA3F7)  # the sequencer's per-state frame timer
    cmd_byte      = _U8(0xA3F9)   # the camera-script command byte (bit6 = a vertical nudge)
    dist_dir      = _U8(0xA3FA)   # 1 if the cursor is left of the player
    dist_x        = _U16(0xA3FB)  # |player_X - cursor_X|
    script_cursor = _U16(0xA3FF)  # the live camera-script cursor (into the 0xA427+ bytecode)
    script_ptr    = _U16(0xA401)  # the active camera-script pointer (script_last detects changes)
    cursor_latch_x = _U16(0xA403)  # cursor pos latched per script command [asm 7585 head]
    cursor_latch_y = _U16(0xA405)
    cam_param_e   = _U16(0xA41F)  # the 5th camera-target param word (no position pair) [asm 93B2]
    cam_target_ptr = _U16(0xA421)  # camera target record-ptr (93CE's di latch)
    target_a      = _U16(0xA423)  # camera target record-ptr A (free 0x19C/0x19D sprites that hit it)
    target_b      = _U16(0xA425)  # camera target record-ptr B
    scroll_push   = _U8(0x91FB)   # camera-boundary bounce push (al = 4 - this) [object_spawn 81F3/8206]
    cursor_x_lo   = _U16(0x91F7)  # the spawn cursor's X clamp bounds [object_spawn 70D7 head]
    cursor_x_hi   = _U16(0x91F9)

    # --- the mode-9 (last-boss) + L6 (tree-boss) script engines [object_spawn 6ADD/6D34] ---
    boss_dwell     = _U8(0xA515)   # the glyph-script dwell/jump countdown (saturating) [6B1C/6BB8]
    boss_cycle     = _U8(0xA516)   # hit-cadence counter (&3; every 4th hit switches script) [6B79]
    boss_script_ptr = _U16(0xA517)  # the live boss glyph-script cursor (also the mode-9 seeded flag, ==0xFFFF
    #                                 before the first frame) [6ADD/6B1C/6B91]
    m9_script_table_ptr = _U16(0xA4F7)  # the mode-9 attack-script advance table cursor [6B1C]
    boss_health    = _U16(0xA519)  # the mode-9 boss health / effect-row spawn-count seed (word views: the
    #                                 0x18 seed, the >>2 spawn-row width, the ==0 death check) [6ADD/6B0F]
    boss_health_lo = _U8(0xA519)   # ... the SAME byte, read/written narrow by the saturating hit-decrement
    #                                 (UNION: byte dec vs word compares — the high byte is always 0) [6B6A-6B74]
    l6_stun   = _U8(0xA324)   # L6 tree-boss hit-stun countdown [6E6D/7015]
    l6_hits   = _U8(0xA325)   # ... hits-per-phase counter (reload 7) [702B/7031]
    l6_anim   = _U8(0xA328)   # ... main-target anim index (cycles 0..2) [6E36/6E5F]
    l6_sub_b  = _U8(0xA329)   # ... sub-target-B table index [6DEE/6E92]
    l6_sub_a  = _U8(0xA32A)   # ... sub-target-A table index [6E12/6EA0]
    l6_timer  = _U8(0xA32B)   # ... spawn timer (saturating-dec each frame; drives the spawn state machine)
    l6_reseed = _U8(0xA32C)   # ... re-seed countdown for l6_timer [6F13]

    # --- the six decoded input flags (DC1's outputs [0x27E8..0x27ED]) ---
    in_fire       = _U8(0x27E8)   # fire/jump held (space/enter sources) [0bc6/58FC]
    in_aux        = _U8(0x27E9)   # the sixth flag (single scancode source 0x2840) — idle-gate input [5D50]
    in_up         = _U8(0x27EA)   # up held [4897]
    in_down       = _U8(0x27EB)   # down held [489B]
    in_right      = _U8(0x27EC)   # right held (drives facing +1 [58BF])
    in_left       = _U8(0x27ED)   # left held (drives facing -1 [58D9])

    # --- DC1's demo-buffer / joystick-gate state (pre2/recovered/input_decode.py) ---
    pending_key    = _U8(0x2874)   # the DC1 pending make-code / demo any-key latch [asm 0DD6]
    demo_ptr       = _U16(0x287A)  # demo buffer cursor (entries are 2 bytes at DS:[ptr+0x3F]) [asm 0DCE]
    demo_byte      = _U8(0x287C)   # current packed input byte (playback) [asm 0E12]
    demo_cnt       = _U8(0x287D)   # remaining repeat count for demo_byte (playback) [asm 0E0E]
    joystick_cfg   = _U8(0x27E4)   # bit7 set => joystick absent [asm 0D6C..0D72]
    joystick_disable = _U8(0x27D9)  # nonzero => joystick absent [asm 0D74..0D79]

    # --- the 247B/25C7 cheat-combo gate: raw scancode-held flags, distinct from the six DECODED input flags ---
    combo_ready_a = _U8(0x2805)   # [asm 247B-2486] one of the three combo-armed flags (al = B & C & A)
    combo_ready_b = _U8(0x2811)   # ...
    combo_ready_c = _U8(0x282C)   # ...
    f1_key        = _U8(0x282F)   # scancode 0x3B (F1) held -- the debug kill-self key [asm 586B]
    f2_key        = _U8(0x2830)   # scancode 0x3C (F2) held -- the debug abort/game-over key [asm 587C]

    # --- front-end scene-wait / menu-dispatch raw scancode-held flags ---
    fire_alt      = _U8(0x2832)   # the secondary fire key (the [0x27e8] | [0x2832] confirm) [native_scene_wait]
    fire_space    = _U8(0x2810)   # the space/enter fire latch [native_menu_flow 8e7b]
    fire_latch    = _U8(0x282D)   # the fire/confirm make-code latch (paired with fire_space)
    key_1_latch   = _U8(0x27F6)   # the '1' make-code latch (level select 1) [asm 9A64]
    key_2_latch   = _U8(0x27F7)   # the '2' make-code latch (level select 2)


#: Back-compat alias — the class began as the collision island's globals and grew into the player's.
CollisionGlobals = PlayerGlobals


class IrisView(DgroupView):
    """The level-end IRIS-CLOSE animation's scratch state (1030:316F/31F4..32DD) — unnamed animation scratch,
    accessor form (native_iris_close)."""

    __slots__ = ()

    step       = _U16(0x2DC0)   # radius shrink step per frame (grows every 0x14 frames)
    accel_ctr  = _U16(0x2DC2)   # the accel counter (bumps step when it reaches 0x14)
    clamp      = _U16(0x2DC4)   # the max(2*Y, 0xF0) radius clamp
    center_y   = _U16(0x2DC6)   # iris centre Y (player screen Y)
    center_x   = _U16(0x2DC8)   # iris centre X (player screen X)
    radius     = _U16(0x2DD0)   # the current radius; != 0 -> native_render composes SceneKind.IRIS


class LoaderGlobals(DgroupView):
    """The asset-loader / boot layout fields (main's 0107..0155 block + the 107B stacking loader) — the
    load-buffer bookkeeping the VM-less cold boot and front end reproduce byte-exactly."""

    __slots__ = ()

    load_top   = _U16(0x2875)   # the SQZ stacking top segment (107B bumps it per load) [107B/0129]
    reset_base = _U16(0x0039)   # the per-level allocation RESET base (= load_top after the front-end assets;
    #                             a restart frees back to here) [asm 0129-012C]
    fg_bank    = _U16(0x003B)   # the FOREGROUND tile-gfx bank segment (the 3721 foliage-in-front pass reads
    #                             it; missing = no foreground) [asm 0123]
    year       = _U16(0x0037)   # the DOS clock year captured at boot (the creators-photo gate: < 0x7CA
    #                             (1994) skips it) [asm 25F6]
    sprites_seg = _U16(0x2DB4)  # the global SPRITES.SQZ bank's load segment [asm 2E12, native_build_sprite_bank]
    # the per-sprite far-pointer tables the global sprite bank writes, indexed by sprite id (native_build_
    # sprite_bank/2EE2-2F2A); 0x200 covers the 0x7190 descriptor table's declared capacity (~460 entries).
    sprite_offset_table = _U16Array(0x5F48, 0x200)   # [asm 2F0E]
    sprite_segment_table = _U16Array(0x62E8, 0x200)  # [asm 2F12]

    # --- per-level loader bookkeeping (1030:3ed6, pre2/native/level_load.py) ---
    union_bank_seg = _U16(0x2DDC)     # the shared/UNION sprite-bank base segment [level_load 4389]
    gfx_group      = _U8(0x2DAA)      # the committed graphics-group id (BACK<group>.SQZ) [asm 3f26]
    filename_digit = _U8(0x2D90)      # the LEVEL<n>.SQZ filename digit char [asm 3f26]
    collect_total_main  = _U16(0x2A74)  # the level's collectible total the tally reads (secret tiles bump it)
    #                                     [asm 3ed0]
    collect_total_decor = _U16(0x2A78)  # the decor-derived collectible count [level_load 4073..40bb]
    bios_seed          = _U16(0xA333)   # the machine-fingerprint seed word (0x20 on the zeroed-BIOS GOG build)
    #                                     [password 932F]
    seed_computed_flag = _U8(0xA335)    # the lazy-init "seed already computed" flag [asm 933c]
    decor_ptr_list = _U16Array(0x6A88, 0x46)   # the decor-assignment pointer list (40bd bubble-sorts it)
    level_start_x = _U16(0x8160)   # the property block's start-point X (player spawn + [0x2DBC]) [level_load]
    level_start_y = _U16(0x8162)   # ... start-point Y
    # the 4 tile-animation lookup tables [asm 42af] the renderer reads to animate tiles -- a pure function of
    # the level's per-tile flags [0x805E] (Tables.ceil_handler), rebuilt once per level load.
    anim_table_0 = _U8Array(0x6688, 0x100)
    anim_table_1 = _U8Array(0x6788, 0x100)
    anim_table_2 = _U8Array(0x6888, 0x100)
    anim_table_3 = _U8Array(0x6988, 0x100)


class SfxTableEntry(StructView):
    """One entry of the per-effect ``{src, len}`` SFX descriptor table (0x1009, stride 4) — the PCM source
    offset + length ``native_play_sfx``'s digital path copies into the active descriptor."""

    __slots__ = ()

    src    = _U16(0)   # PCM source offset into the sample bank [asm 02b7]
    length = _U16(2)   # PCM length [asm 02be]


class AudioGlobals(DgroupView):
    """The audio-command state (``play_sfx`` dispatch + the song loader) — ``pre2/native/audio.py`` reproduces
    these byte-exact so the enhanced player's per-frame poll can read them."""

    __slots__ = ()

    sfx_src = _U16(0x1004)          # the active SFX descriptor: PCM source offset [asm 02b7]
    sfx_len = _U16(0x1006)          # ... PCM length [asm 02be]
    sfx_pcspk_note = _U16(0x1035)   # the PC-speaker active-note pointer [asm 0292]
    sfx_sample_seg = _U16(0x0B59)   # the SFX PCM sample-bank segment [asm 07C9]
    song_length = _U8(0xDC2)        # the loaded song's order-table length [asm 22FE]
    sfx_entries = StructArray(0x1009, 4, 11, SfxTableEntry)   # the 11-effect {src,len} table [asm 02a9]


class PasswordScreenView(DgroupView):
    """The ENTER-CODE front-end screen's state (1030:9985/99AA..9ADF) — front_end.py's ``_password_init``/
    ``_password_step``."""

    __slots__ = ()

    code        = _U16(0xB1B9)   # the accumulated 16-bit code (the current 4-hex group)
    hist0       = _U16(0xB1B3)   # [asm 9A2E-9A36] the rolling history of the 3 prior groups (cheat buffer)
    hist1       = _U16(0xB1B5)
    hist2       = _U16(0xB1B7)
    echo        = _U8Array(0xB170, 4)   # the 4-char on-screen code buffer ("[[[[" when empty)
    cursor      = _U16(0xB1A8)   # echo cursor / entered-char count (0..4)
    status      = _U8(0xB1AA)    # [asm 9AAA/9A94] 0 = accepting input, 1 = wrong-code feedback pause
    wrong_timer = _U16(0xB1B0)   # [asm 9AB4] frames elapsed in the wrong-code pause
    bios_seed   = _U16(0xA333)   # the machine-fingerprint seed word (0x20 on the zeroed-BIOS GOG build) [932F]


class RngView(DgroupView):
    """The game's two PRNG state blocks, NAMED — replaces the 4-line read/advance/write-back boilerplate at
    every roll site with ``rng.roll()``. The generator MATH stays in pre2/recovered/prng.py (this view is
    layout + wiring only). Bind it to a read-through backend (an island overlay) when several rolls happen in
    one pass — each roll must see the previous roll's state."""

    __slots__ = ()

    lcg_a  = _U8(0x2CEC)    # [asm 39DF] the four-byte mixing generator's state ...
    lcg_b  = _U8(0x2CED)
    lcg_c  = _U8(0x2CEE)
    lcg_d  = _U16(0x2CEF)
    ror    = _U16(0x28C1)   # [asm 26CF] the one-word rotate generator's state

    def roll(self) -> int:
        """Advance the LCG (``1030:39DF``), write the new state back through the backend, return ``AL``."""
        from pre2.recovered.prng import rng_lcg
        a, b, c, d, ret = rng_lcg(self.lcg_a, self.lcg_b, self.lcg_c, self.lcg_d)
        self.lcg_a = a
        self.lcg_b = b
        self.lcg_c = c
        self.lcg_d = d
        return ret

    def roll_ror(self) -> int:
        """Advance the rotate generator (``1030:26CF``), write it back, return the new word (== the state)."""
        from pre2.recovered.prng import rng_ror
        new = rng_ror(self.ror)
        self.ror = new
        return new


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
    script_table  = _U16Array(0x2D40, 0x10)    # per-level scroll-script source table: ptr = [0x2D40+level*2]
    #                               [native_5237 5237/52b3]

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


# ---- proximity-scenery triggers (53F6 scan / 5427 map-mod / 41CA bank build / 52D2 restore) -----------------

class ProximityTrigger(StructView):
    """One earthquake/breakable-scenery trigger — a stride-0xA entry of the 15-entry ``[0x83F3]`` table.

    ``block_top`` is the map offset of the collapsible block's CURRENT top row (the map-mod moves it up
    0x100/row as the block rises); ``trigger_pos`` is the packed player-tile coordinate that arms it
    (0xFFFF = inactive, 0xFFFE = fired — then the map-mod runs each frame until ``countdown`` hits 0);
    ``reveal_cursor`` walks BACKWARD through the 41CA-saved pristine rows in the ``[0x2875]`` bank, one
    ``width`` per fire."""

    __slots__ = ()

    FIRED    = 0xFFFE
    INACTIVE = 0xFFFF

    block_top     = _U16(0)
    width         = _U8(2)
    height        = _U8(3)
    trigger_pos   = _U16(4)
    reveal_cursor = _U16(6)
    countdown     = _U8(8)

    @property
    def dead(self) -> bool:
        """No trigger in this entry at all (the load left ``block_top`` = 0xFFFF)."""
        return self._backend.rw(self._base) == 0xFFFF


class ProximityView(DgroupView):
    """The proximity-scenery island's DGROUP state: the trigger table + the frame gate + the camera shake it
    kicks, and the two segment registers its map/bank halves address."""

    __slots__ = ()

    triggers  = StructArray(0x83F3, 0xA, 15, ProximityTrigger)
    tick      = _U8(0x6BD5)     # the free-running frame counter (the map-mod's &3 every-4th-frame gate)
    shake     = _U8(0x6BEA)     # camera-shake magnitude (set to 7 while a trigger is armed-in-range/fired)
    map_seg   = _U16(0x2DDA)    # the level-map segment (collision tiles — the state 5427/52D2 mutate)
    bank_seg  = _U16(0x2875)    # the 41CA save bank (pristine block rows; 52D2's restore source)


class LightFadeView(DgroupView):
    """The dark-cave light-fade state (6772 / the light pickups 876C/8790): the two direction flags, the
    per-tick step, and the resting lights-off bit — plus what the DAC ramp derives from (the level id and the
    palette bytes, read via :meth:`palette_byte`)."""

    __slots__ = ()

    DARK_PALETTE = 0xACB7       # the fixed "lights off" 16-colour palette [asm 6791]

    to_dark    = _U8(0x6C01)    # fade toward the dark palette (set by the light-OFF pickup)
    to_light   = _U8(0x6C02)    # fade back toward the level palette
    step       = _U8(0x6C03)    # the ramp step, ++ per game tick while a fade is active [asm 677B]
    lights_off = _U8(0x6C04)    # resting state after a completed fade-to-dark
    level      = _U8(0x2D8A)

    @property
    def active(self) -> bool:
        return bool(self.to_dark | self.to_light)

    @property
    def level_palette(self) -> int:
        """DGROUP offset of the current level's 0x30-byte palette [asm 677F-6787]."""
        return self._backend.rw(PALETTE_PTR_TABLE + self.level * 2)

    def palette_byte(self, base: int, k: int) -> int:
        """One 6-bit DAC channel byte from a palette at DGROUP offset ``base``."""
        return self._backend.rb((base + k) & 0xFFFF)


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
    life   = _U8(0x11)     # anim/life counter (decremented; blink-gated) — read by the sprite renderer

    @property
    def sprite_id(self) -> int:
        """The bare sprite index (``sprite`` with the flag bits masked off)."""
        return self._backend.rw(self._base + 4) & 0x1FFF


class DebrisSlot(StructView):
    """One slot of the 16-entry debris/effect pool (0x5450, stride 0x12) the 60DF lifetime tick walks. Free when
    ``sprite`` [+4] is 0xFFFF; each active slot decrements ``timer`` [+2] and ``lifetime`` [+0xC], freeing the
    slot when the lifetime hits exactly 0. Names the three word fields the tick touches."""

    __slots__ = ()

    timer    = _U16(2)     # [+2] the secondary per-slot countdown
    sprite   = _U16(4)     # [+4] sprite id; 0xFFFF = free slot
    lifetime = _U16(0xC)   # [+0xC] lifetime; slot frees when this reaches 0


class PopupSlot(StructView):
    """One slot of the 5-entry popup/score ring (walked down from head [0x6BBE], wrapping 0x4F76->0x4FBE,
    stride 0x12) the 581E tick advances. ``timer`` [+2] decrements; ``anim`` [+4] (masked, then +1) frees the
    slot to 0xFFFF once it reaches 0x3A."""

    __slots__ = ()

    timer = _U16(2)   # [+2] per-slot countdown
    anim  = _U16(4)   # [+4] anim id (low 0x1FFF); 0x3A -> free (0xFFFF)


class ObjectDef(StructView):
    """The object TYPE-DEFINITION record (pointed to by ``ObjectSlot.def_ptr``) the object walker reads each
    frame. It is a per-type UNION read as raw bytes (the AI handlers reconstruct words where needed), so the
    fields keep offset-placeholder names (``d2``/``d4``/...) until each type's interpretation is reversed —
    naming the record STRUCTURALLY (offset-free access) without over-claiming per-type meaning."""

    __slots__ = ()

    d1  = _U8(1)
    d2  = _U16(2)
    d4  = _U8(4)
    d6  = _U8(6)
    d7  = _U8(7)
    d8  = _U8(8)
    d9  = _U16(9)
    dB  = _U16(0xB)
    dD  = _U8(0xD)
    dE  = _U8(0xE)
    dF  = _U8(0xF)
    d10 = _U8(0x10)
    d11 = _U8(0x11)
    d12 = _U8(0x12)
    d13 = _U8(0x13)
    d14 = _U8(0x14)


class AttackPhaseEntry(StructView):
    """One entry of the 0x7B04 attack-phase table (stride 5) — cyxx-unnamed. Indexed by
    ``PlayerGlobals.attack_phase``; ``player_state_attack``/``_attack_render_sprite``/``_attack_spawn`` read it
    for the phase's frame-table pointer, sound effect, damage/tolerance value, and dispatch flag."""

    __slots__ = ()

    frametbl_ptr = _U16(0)   # -> the {frame, sprite_id, x_off, y_off} render-sprite frame table [asm 6081]
    sfx          = _U8(2)    # play_sfx dl for the sound-path branch [asm 5FD2]
    v19          = _U8(3)    # damage/hit-tolerance value (x4 if PlayerGlobals.charge) [asm 5FB1]
    flag         = _U8(4)    # bit6 selects render-sprite vs sound path; bit0 gates the projectile spawn [asm 5FC3/600F]


class EffectSource(StructView):
    """One entry of the on-screen effect/particle SOURCE list (0x8F1D, stride 7) — the record
    ``project_particles`` (8922) animates and projects into a render slot. This is cyxx `level_item_t`
    (MAX_LEVEL_ITEMS 70 == our SRC_COUNT). Names its 4 fields so the projection walk speaks the source's
    vocabulary (``src.x`` / ``src.sprite`` / ``src.bounce``) instead of raw ``rw(si)`` / ``rw(si+4)`` /
    ``rb(si+6)``. ``bounce`` [+6] is the signed bounce-phase byte (the caller sign-extends it)."""

    __slots__ = ()

    x      = _U16(0)   # world X (>>4 = screen column). cyxx level_item_t.x_pos
    y      = _U16(2)   # world Y; the bounce animation writes the new value back here. cyxx level_item_t.y_pos
    sprite = _U16(4)   # sprite id; 0xFFFF = empty source entry. cyxx level_item_t.spr_num
    bounce = _U8(6)    # bounce-phase counter (read signed, ping-pongs at +/-4). cyxx level_item_t.y_delta


class BonusCellSlot(StructView):
    """One record of the 80-cell bonus/collectible list (0x8C8D, stride 5) — cyxx ``level_bonus_t``."""

    __slots__ = ()

    hidden_tile = _U8(0)  # the secret-tile id level_load's 3ead swaps into the map cell on load
    #                        [level_load _self_patch_secret_tiles]
    tile_id = _U8(1)  # the underlying tile id restored into the level map on collect [combat_interaction 8B81]
    counter = _U8(2)  # bit7 = counter-bonus flag; else &0x40 selects the two-burst/single-burst popup family
    #                    [combat_interaction bonus_hit_handler 8A5A/8B66]
    cell = _U16(3)   # packed (y_cell << 8) | x_cell; 0xFFFF = empty/collected. cyxx level_bonus_t.pos


# ---- the player (the 58A7 FSM's struct at 0x4F1C — literally render slot #1 with kinematics appended) -------

RENDER_SLOTS_BASE = 0x4F0A     # slot 0; the player is slot 1 (base + 0x12 = 0x4F1C)
PLAYER_BASE = 0x4F1C


class PlayerView(RenderSlot):
    """The player struct — render slot #1 (so ``x``/``y``/``sprite``/``flags`` are inherited: the player's
    on-screen record IS its slot; ``sprite`` is the packed anim-frame word, 0x2000 flag included) plus the
    58A7 FSM's kinematics fields appended after it.

    **Width-alias convention** (the "union" answer): when the ASM reads the same bytes at different widths,
    each width gets its OWN named field, because a different width is a different *semantic* — ``facing`` is
    the signed +1/-1 word the FSM integrates with; ``facing_lo`` is the low byte the anim mirror passes to
    ``player_advance_anim``. Same storage, two meanings, two names — never a width argument at the call site.

    ``death_state`` [+0x11, 0x4F2D] aliases the generic slot's ``life`` byte — for the PLAYER that byte is the
    death/hurt state the respawn logic reads, so it carries the player-specific name here."""

    __slots__ = ()

    xvel        = _S16(0x06)    # 0x4F22 — X velocity, 12.4 fixed [asm 5A0F integrate]
    move_flag   = _U8(0x07)     # 0x4F23 — high byte of xvel (width alias): bit7 = moving-left sign
    #                              [camera_scroll _h_init 57FD — the horizontal-follow direction pick]
    motion_mode = _U8(0x08)     # 0x4F24 — kinematics mode/shift (friction = 0xC >> mode; launchers set 2/3)
    facing      = _S16(0x09)    # 0x4F25 — +1 / -1 (the FSM's word)
    facing_lo   = _U8(0x09)     # 0x4F25 low byte — the anim-mirror flag (width alias of ``facing``)
    anim_b      = _U8(0x0B)     # 0x4F27 — anim B-state (anim id memory; also the camera-shake gate input)
    anim_ptr    = _U16(0x0C)    # 0x4F28 — current anim-script pointer (638B advances it)
    yvel        = _S16(0x0E)    # 0x4F2A — Y velocity, 12.4 fixed [asm 5A36 integrate]
    run_flag    = _U8(0x10)     # 0x4F2C — run state (reset on an anim change)
    death_state = _U8(0x11)     # 0x4F2D — death/hurt state byte (aliases RenderSlot.life for the player slot)

    def __init__(self, source):
        super().__init__(_coerce_backend(source), PLAYER_BASE)

    @property
    def slot0(self) -> RenderSlot:
        """Render slot 0 (just below the player slot) — its ``sprite``=0xFFFF is the 'suppress the normal
        player draw' switch the death bounce flips [asm 50DF]."""
        return RenderSlot(self._backend, RENDER_SLOTS_BASE)

    @property
    def tile_coords(self) -> int:
        """The packed player TILE coordinate ``(sar(y,4)&0xFF)<<8 | (sar(x,4)&0xFF)`` — what the trigger
        tables match against [asm 549A]. Arithmetic (sign-preserving) shifts exactly like the ASM ``sar``."""
        def sar4(v: int) -> int:
            v &= 0xFFFF
            if v & 0x8000:
                v -= 0x10000
            return (v >> 4) & 0xFF
        return ((sar4(self._backend.rw(self._base + 2)) << 8)
                | sar4(self._backend.rw(self._base))) & 0xFFFF


class ProjectileSlot(RenderSlot):
    """One thrown-weapon projectile slot (the 4-slot list at 0x4F2E, stride 0x12) — free when ``sprite`` is
    0xFFFF. The attack handler spawns the club hitbox into the first free one [asm 627D/6017-6070]; the
    camera-target scans test each active one [80E6/6FDE]."""

    __slots__ = ()

    xoff      = _U16(0x06)   # the spawn record's X offset word (facing-negated) [asm 604E]
    kind      = _U8(0x08)    # (phase flag >> 1) & 3 [asm 601C]
    spawn_ptr = _U16(0x0C)   # the spawn record ptr (past the frame table's terminator) [asm 6030]
    yoff      = _U16(0x0E)   # the spawn record's Y offset word [asm 603B]
    # in-flight aliases: once launched, the 6210 physics tick reinterprets the same words as kinematics
    alive     = _U8(0x05)    # width alias of RenderSlot.flags: bit 0x20 = alive, else free the slot [asm 621C]
    xvel      = _U16(0x06)   # width alias of xoff: X velocity, 12.4 fixed [asm 6229]
    facing    = _U8(0x07)    # width alias of xvel's high byte: the facing byte (bit7) [asm 6256]
    anim_ptr  = _U16(0x0C)   # width alias of spawn_ptr: the live anim-script cursor [asm 6244]
    yvel      = _U16(0x0E)   # width alias of yoff: Y velocity [asm 6236]
    # the attract-title scene (8F2D..9046) reuses this SAME 4-slot arena for the dino / bounce objects,
    # walked by the generic 9047 anim stepper instead of the projectile physics -- another genuine union.
    flip_byte = _U8(0x09)    # width alias of RenderSlot.source's low byte: the facing/flip flag [asm 905D]

    @property
    def free(self) -> bool:
        return self.sprite == 0xFFFF                        # [asm 6285/6FE4] the free/active test


class WallMarker(StructView):
    """One 8-byte wall-impact marker (the list at 0x6EA9); free when the leading word is 0x55AA [asm 64FA]."""

    __slots__ = ()

    x  = _U16(0)             # player X << 3 [asm 6505-650A]
    y  = _U16(2)             # player Y << 3 [asm 650C-6511]
    b4 = _U8(4)              # zeroed on registration [asm 6514]
    b5 = _U8(5)              # zeroed [asm 6518]
    b7 = _U8(7)              # zeroed [asm 651C]

    @property
    def free(self) -> bool:
        return self.x == 0x55AA                             # [asm 64FD]


class CaveTriggerEntry(StructView):
    """One entry of the 20-slot cave/teleport-entrance trigger table (0x8367, stride 7) [native_trigger_scan
    5316; native_cave_teleport 5326]."""

    __slots__ = ()

    source    = _U16(0)   # matched against the player's packed tile coord [asm 531C]
    dest_cam  = _U16(2)   # destination camera (packed lo=X, hi=Y)
    dest_tile = _U16(4)   # destination player tile
    flag      = _U8(6)    # -> PlayerGlobals.unk_6BD9 (the scroll-follow gate) [asm 5391]


class L6Projectile(StructView):
    """One level-6 tree-boss falling projectile (the 5-slot list at 0x7DAF, stride 0xB) — free when
    ``sprite`` is 0xFFFF. Integrated per frame (Y by ``fall_vel>>4``, X by the oscillating drift
    accumulator) and projected into its 0x55EE render slot [asm 6D40..6DDD / 6E92..6F37]."""

    __slots__ = ()

    x          = _U16(0x0)   # world X [asm 6DB7]
    y          = _U16(0x2)   # world Y [asm 6D80]
    sprite     = _U16(0x4)   # anim/id; 0xFFFF = free [asm 6D4F/6ED4]
    fall_vel   = _U16(0x6)   # Y velocity, 12.4 fixed [asm 6EF6/6D80]
    drift_acc  = _U16(0x8)   # the oscillating X-drift accumulator [asm 6D8B/6F05]
    drift_step = _U8(0xA)    # per-frame drift step, negated at the +/-0x20 extents [asm 6D95/6F0A]

    @property
    def free(self) -> bool:
        return self.sprite == 0xFFFF                     # [asm 6D4F/6EC1]


class EffectParticle(RenderSlot):
    """The 60FE PHYSICS interpretation of the same stride-0x12 slot family — the 0x50A8 pool when it holds
    bounce/debris particles and boss bolts (emitters: combat_interaction's burst, the boss-script spawners).
    A genuine UNION with :class:`ObjectSlot`: the object walker keeps velocities at +8/+0xA, the effects
    pass keeps them at +6/+0xE — which record class applies is decided by the pass that ticks the slot."""

    __slots__ = ()

    xvel     = _U16(0x06)   # X velocity, 12.4 fixed [asm 6229: X += Xvel>>4]
    facing   = _U8(0x07)    # width alias of xvel's high byte: the facing/flip byte [effects_update]
    lifetime = _U16(0x0C)   # signed lifetime countdown; doubles as the anim selector [asm 612B/61F9]
    yvel     = _U16(0x0E)   # Y velocity (gravity +9/frame, capped 0x100) [asm 615C-615F]
    substate = _U8(0x11)    # width alias of RenderSlot.life: the particle substate byte [effects_update]


class ObjectSlot(RenderSlot):
    """One active object/enemy record (the 12-slot list at 0x4FD0 + the 32-slot effect/burst pool at 0x50A8,
    stride 0x12) — the object walker's own view of the same 0x12-stride render-record family. Fields per
    object_tick's byte-exact view (684E): free when ``sprite`` (the id word) is 0xFFFF."""

    __slots__ = ()

    def_ptr  = _U16(0x06)    # -> the type-definition record (the read-only def table) [asm: [di+6]]
    xvel     = _U16(0x08)    # X velocity, 12.4 fixed [object_tick _obj_view]
    flip_byte = _U8(0x09)    # xvel's high byte -- the animation flip/facing flag [object_update advance_animation]
    yvel     = _U16(0x0A)    # Y velocity, 12.4 fixed
    anim_ptr = _U16(0x0C)    # the anim-script cursor
    state    = _U8(0x0E)     # the behavior state byte (0xFF = dead; handlers dispatch on it) [asm 8310/8CB7]
    hp       = _U8(0x0F)     # hit points; the damage handler subtracts into it [asm 8C48]
    hits     = _U8(0x10)     # hit accumulator: the hurt handler reads >>2/>>3 and caps [asm 834E/8C7A]


class TerrainEntity(StructView):
    """One terrain / moving-entity SOURCE record (the 16-slot list at 0x9107, stride 0xF; 4907's walk).
    Free when ``sprite`` is 0xFFFF. Two movement families share the record: type-8 fall/settle uses the
    ``fall_vel`` word at +0xB; the 8-direction platform uses ``osc`` (its high byte) as the oscillation
    counter — the established two-names-for-two-widths convention."""

    __slots__ = ()

    x            = _U16(0x00)   # world X (12.4-ish; column = >>4) [asm 498F/4A67]
    y            = _U16(0x02)   # world Y [asm 4936/49EA]
    sprite       = _U16(0x04)   # sprite id; 0xFFFF = free slot [asm 4AF0 walk]
    type_dir     = _U8(0x06)    # &0xF type (8 = faller); &7 platform heading; bit6 = hold [asm 4950/49D5]
    speed_ramp   = _U8(0x07)    # the ramping speed/accel byte (toward ``speed``) [asm 4942/4976/4A0D]
    unk_8        = _U8(0x08)    # not accessed by the recovered pass; role not yet evidenced
    settle_reload = _U8(0x09)   # reload value for ``settle_timer`` [asm 49E0]
    state        = _U8(0x0A)    # type-8 state machine: 0 wait/brake, 1 fall, 2 settle [asm 4939]
    fall_vel     = _U16(0x0B)   # type-8 fall velocity word [asm 4936/4988]
    osc          = _U8(0x0C)    # width alias of fall_vel's high byte: the platform oscillation counter
    settle_timer = _U8(0x0D)    # settle frame countdown [asm 4956/49CF]
    speed        = _U8(0x0E)    # the platform's target speed [asm 49FB/4A0D]


class CamTarget(StructView):
    """One camera-target geometry record (the 4-slot stride-6 block at 0xA407 the camera script fills):
    param word (bit15 flips when facing) + target X/Y [asm 93B2 block, object_spawn camera_target_geometry]."""

    __slots__ = ()

    param = _U16(0)
    x     = _U16(2)
    y     = _U16(4)


class Particle(StructView):
    """One 4B8E particle (the 20-slot array at 0x7DE6, stride 6): X.w Y.w angle.b speed.b; the X word is
    0xFFFF when the slot is dead [asm 4C1D]."""

    __slots__ = ()

    x     = _U16(0)
    y     = _U16(2)
    angle = _U8(4)           # indexes the cos/sin tables (0x6F90/0x7090)
    speed = _U8(5)

    @property
    def dead(self) -> bool:
        return self.x == 0xFFFF                          # [asm 4C1D]


# ---- the fixed slot LISTS (StructArrays: indexed named records instead of pointer walks). Attached to
# PlayerGlobals here because RenderSlot/ProjectileSlot are defined below it in the file. ----
PlayerGlobals.render_slots = StructArray(0x4F0A, 0x12, 116, RenderSlot)     # the on-screen records
#     (0x4F0A..0x5732, slot 1 = the player); 116 = the span the forward oracle masks
PlayerGlobals.projectiles = StructArray(0x4F2E, 0x12, 4, ProjectileSlot)    # the 4 thrown-weapon slots [627D]
PlayerGlobals.effect_sources = StructArray(0x8F1D, 7, 0x46, EffectSource)   # the 70-entry effect-source list
PlayerGlobals.bonus_cells = StructArray(0x8C8D, 5, 0x50, BonusCellSlot)     # the 80-cell bonus/collectible list
PlayerGlobals.item_snapshot = _U16Array(0x6C12, 0x47)    # native_4f6c's effect-source snapshot (0x46 + end marker)
PlayerGlobals.active_flag_snapshot = _U8Array(0xA2A8, 0x50)   # native_4f6c's respawn active-flag snapshot
PlayerGlobals.item_queue = _U8Array(0x6C12, 0x71)   # the collected-item queue / per-type count table [asm 51e2]
PlayerGlobals.cave_triggers = StructArray(0x8367, 7, 20, CaveTriggerEntry)  # the cave/teleport trigger table
PlayerGlobals.trail_ring_slots = StructArray(0x4F76, 0x12, 5, RenderSlot)   # the trail/dust ring (the cursor
#     g.trail_ring walks DOWN with wrap 0x4F76 -> 0x4FBE) [5E2E-5E37]
PlayerGlobals.effect_row = StructArray(0x56A2, 0x12, 8, RenderSlot)         # the 7585 effect/boss-health row
PlayerGlobals.wall_markers = StructArray(0x6EA9, 8, 20, WallMarker)         # the 64FA wall-impact list
PlayerGlobals.l6_projectiles = StructArray(0x7DAF, 0xB, 5, L6Projectile)    # the L6 tree-boss projectiles
PlayerGlobals.l6_render_slots = StructArray(0x55EE, 0x12, 5, RenderSlot)    # ...their projected render slots
PlayerGlobals.boss_targets = StructArray(0x5648, 0x12, 5, RenderSlot)       # the boss/camera-target records
PlayerGlobals.enemy_slots = StructArray(0x4FD0, 0x12, 12, ObjectSlot)       # the 12 active object/enemy slots
PlayerGlobals.burst_slots = StructArray(0x50A8, 0x12, 32, ObjectSlot)       # the effect/score-burst pool
PlayerGlobals.effect_particles = StructArray(0x50A8, 0x12, 32, EffectParticle)  # the SAME pool, 60FE physics view
#     (0x50A8..0x52E8 — the free pool after the 12 main objects; same record family)
PlayerGlobals.particles = StructArray(0x7DE6, 6, 20, Particle)              # the 4B8E particle array
PlayerGlobals.terrain_entities = StructArray(0x9107, 0xF, 16, TerrainEntity)  # the 4907 source list
PlayerGlobals.cam_targets = StructArray(0xA407, 6, 4, CamTarget)            # the camera-script geometry block

#: Named MUTABLE regions that are dynamic-record ARENAS, not fixed-stride arrays — the flip keeps each as an
#: owned byte-buffer behind its manager (the variable-stride record layout lives with the owning island).
ARENAS = {
    # the stride-terminated list ([si] <= 0x32 walks; level_load 4182) ends before the sprite-bank
    # words at 0x8C89 -- that is the arena's hard bound; corpus write high-water 0x8845
    "entity_arena (the 2nd-pass live-entity records; entry 0 = the player; variable stride)": (0x8489, 0x8C88),
}
#     (x/y/sprite per record; +5 = the flash flags byte; boss_x/boss_y alias record 0's x/y) [6E42/7113]


# ---- FieldBackend: the name-covered store (the field-backed seam's shipped half) ----------------------------

def _named_map():
    """offset -> the covering field's ``(name, base_offset, width)``, harvested from THIS module's
    declarations (every DgroupView subclass + PlayerView + their StructArrays). Cached; built lazily so the
    walk sees the post-class array attachments."""
    import sys
    mod = sys.modules[__name__]
    if getattr(mod, "_NAMED_MAP", None) is not None:
        return mod._NAMED_MAP

    def field_descs(cls):
        seen = set()
        for klass in cls.__mro__:
            for name, d in vars(klass).items():
                if name not in seen and isinstance(d, (_U8, _S8, _U16, _S16)):
                    seen.add(name)
                    yield name, d.off, (2 if isinstance(d, (_U16, _S16)) else 1)

    out: dict[int, tuple[str, int, int]] = {}

    def mark(name, off, width):
        for k in range(width):
            out.setdefault((off + k) & 0xFFFF, (name, off & 0xFFFF, width))

    for cls_name in dir(mod):
        cls = getattr(mod, cls_name)
        if not (isinstance(cls, type) and issubclass(cls, StructView)):
            continue
        base = 0 if (issubclass(cls, DgroupView) and cls is not DgroupView) else \
            (PLAYER_BASE if cls is PlayerView else None)
        if base is None:
            continue
        for fname, off, width in field_descs(cls):
            mark(f"{cls_name}.{fname}", base + off, width)
        for aname, d in vars(cls).items():
            if isinstance(d, StructArray):
                for i in range(d.length):
                    ebase = base + d.off + i * d.stride
                    for fname, off, width in field_descs(d.struct_cls):
                        mark(f"{cls_name}.{aname}[{i}].{fname}", ebase + off, width)
    for label, (lo, hi) in ARENAS.items():
        for o in range(lo, hi + 1):
            out.setdefault(o, (label, lo, hi - lo + 1))
    mod._NAMED_MAP = out
    return out


_NAMED_MAP = None


class FieldBackend:
    """The NAME-COVERED state store: the same offset-keyed backend API as :class:`ByteBackend`, but storage
    is sparse and every access must land inside a DECLARED field or arena — anything else raises (fail-loud).

    This is the field-backed seam's shipped half: pure named-state code (and its tests) can run over it with
    no memory image at all, and any hidden raw-offset dependency outside the declarations surfaces as an
    immediate ``KeyError`` instead of silently reading a zero. The bridge's serializer
    (``pre2.bridge.state_fields``, built on the MACHINE-GENERATED registry) converts a live image to/from the
    same named space; scripts/verify_field_flip.py is the corpus proof that the named space covers every
    mutated gameplay byte."""

    _IS_DGROUP_BACKEND = True
    __slots__ = ("_bytes", "_map")

    def __init__(self, seed=None):
        self._map = _named_map()
        self._bytes: dict[int, int] = {}
        if seed is not None:                                   # a whole image / NativeGameState
            data = getattr(seed, "data", seed)
            for o in self._map:
                self._bytes[o] = data[DGROUP_BASE + o]

    def _check(self, off):
        if off not in self._map:
            raise KeyError(f"FieldBackend: offset 0x{off:04X} is outside every declared field/arena")

    def rb(self, off: int) -> int:
        off &= 0xFFFF
        self._check(off)
        return self._bytes.get(off, 0)

    def wb(self, off: int, v: int) -> None:
        off &= 0xFFFF
        self._check(off)
        self._bytes[off] = v & 0xFF

    def rw(self, off: int) -> int:
        return self.rb(off) | (self.rb((off + 1) & 0xFFFF) << 8)

    def ww(self, off: int, v: int) -> None:
        self.wb(off, v & 0xFF)
        self.wb((off + 1) & 0xFFFF, (v >> 8) & 0xFF)

    def owner(self, off: int) -> str:
        """The declared name covering ``off`` — the readable answer to 'what is this byte?'."""
        return self._map[off & 0xFFFF][0]


class HybridBackend:
    """The field-backed flip's store: named DGROUP bytes live in a :class:`FieldBackend`, everything else
    (read-only tables, the tilemap pointer, boot constants, the untouched span) stays in a residue byte image.

    This is the product's state-of-record once flipped — there is no single contiguous mutable image; the
    named mutable state has no offset home, only the residue keeps bytes. It presents the same offset-keyed
    rb/rw/wb/ww API, so ``NativeGameState.backend`` swaps to it with nothing else changing: every read of a
    named offset resolves to the FieldBackend, every write of a named offset lands there and NOT in the image.
    (The residue image is still needed for the read-only content until Phase 5 migrates it to loaded objects.)

    ``materialize(data)`` writes the FieldBackend's named bytes back over an image — the serialisation the
    renderer and the verifier need (a contiguous DGROUP image), proving the split is lossless."""

    _IS_DGROUP_BACKEND = True
    __slots__ = ("fields", "_img", "_named")

    def __init__(self, field_backend: "FieldBackend", residue_data):
        self.fields = field_backend
        self._img = residue_data                               # the 1 MB image (its named-offset bytes go stale)
        self._named = field_backend._map                      # offset -> covering (name, base, width)

    def rb(self, off: int) -> int:
        off &= 0xFFFF
        if off in self._named:
            return self.fields._bytes.get(off, 0)
        return self._img[DGROUP_BASE + off]

    def wb(self, off: int, v: int) -> None:
        off &= 0xFFFF
        if off in self._named:
            self.fields._bytes[off] = v & 0xFF
        else:
            self._img[DGROUP_BASE + off] = v & 0xFF

    def rw(self, off: int) -> int:
        return self.rb(off) | (self.rb((off + 1) & 0xFFFF) << 8)

    def ww(self, off: int, v: int) -> None:
        self.wb(off, v & 0xFF)
        self.wb((off + 1) & 0xFFFF, (v >> 8) & 0xFF)

    def materialize(self, data=None) -> None:
        """Write the FieldBackend's named bytes back over ``data`` (default: the residue image) so it becomes
        a full contiguous DGROUP image again — for the renderer / the byte-exact digest."""
        data = self._img if data is None else data
        for off, val in self.fields._bytes.items():
            data[DGROUP_BASE + off] = val & 0xFF
