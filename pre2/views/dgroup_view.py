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
    field registry instead of a byte image, and this module's offset map moves to the detachable bridge."""
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

    @property
    def offset(self) -> int:
        """The struct's DGROUP base offset — TRANSITIONAL interop for the offset-keyed helpers a slot is
        passed to during the field-backed migration (e.g. ``hitbox_overlap(rb, rw, slot.offset, di)``).
        New code should pass the VIEW, not the offset; this property is the migration seam, not the API."""
        return self._base


def _coerce_backend(source):
    """A backend passes through — the package's own backends plus anything marked ``_IS_DGROUP_BACKEND``
    (island-local overlays like player_collision's read-through ``_Overlay`` opt in with that attribute; the
    VM ``mem`` object must NOT pass, its ``rb`` takes (seg, off)). Anything else (NativeGameState / VM ``mem``
    / raw ``bytearray``) is wrapped in a :class:`ByteBackend`."""
    if isinstance(source, (ByteBackend, SegmentBackend, OverlayBackend, WidthContractBackend, DictBackend)) \
            or getattr(source, "_IS_DGROUP_BACKEND", False):
        return source
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
    current_object = _U16(0x6BB1)  # the FSM's current-object pointer; NULL on the player's own collision [6698]
    dipping_tile  = _U16(0x6BAB)  # map offset of the currently-sagging bridge tile; 0x55AA = none [5BBB/5BF4]
    grid_dirty    = _U8(0x2DF4)   # whole-grid redraw request [5C82]
    grid_dirty_token = _U16(0x2DE0)  # its 0x55AA companion token [5C87]
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
    display_page  = _U16(0x2DD6)  # the CRTC display-start PAGE the present flips [2DD6; read at every present]
    input_source  = _U8(0x2879)   # DC1's source: 0 live keyboard / 1 demo-attract playback / 2 record [0DC1]
    level_end_mode = _U8(0x6BE6)  # the 4C69 level-end dispatch mode: 1 normal end / >1 warp [4C69/4C74]
    level         = _U8(0x2D8A)   # the current level index [5B18]; 0xFF = none chosen yet [8ee9]
    mode          = _U8(0xB197)   # 0 = BEGINNER / 1 = EXPERT — the mode-select toggle [9941/8ee9]
    mode_copy     = _U8(0xB198)   # the committed copy the loader reads [994E]
    attract_mode  = _U8(0x083D)   # the attract-demo header's mode byte (set with the commit) [994E]
    attract_level = _U8(0x083E)   # the attract/default level header [8E98]
    level_flags   = _U8(0x8166)   # bit0 = suppress the hard-land bounce [64BA]; bit1 = no idle camera-pan
    #                               [5D95]; bit2 = top-kill fence [5AF1]
    unk_6BFE      = _U8(0x6BFE)   # post-worker: nonzero -> the 64DF soft-land tail instead of air physics
    #                               [5B38]; zeroed by the jump body [5F41]; gates the attack Yvel nudge [6004]

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
    hit_flag      = _U8(0xA330)   # hitbox_overlap's vertical-detail hit flag (1 = registered) [8D7B]
    hit_pass_full = _U8(0xA312)   # set across a projectile/player pass -> 8D7B uses the FULL (un-halved)
    #                               tolerance [6FCF/6FD7]
    hit_debounce  = _U16(0xA3FD)  # frame stamp of the last camera-target hit (debounce window 0x16) [8109]
    boss_phase    = _U16(0xA326)  # the L6/boss fight phase (advances every 7 hits) [7036/7DA9]
    boss_x        = _U16(0x5648)  # the boss/camera-target record origin X [74A8/7041]
    boss_y        = _U16(0x564A)  # ... origin Y [74B1/704A]

    collected_linked = _U16(0x2A7A)  # the LINKED-item collected count (tally percent = [0x2A76]+this) [85CC/5139]

    # --- the six decoded input flags (DC1's outputs [0x27E8..0x27ED]) ---
    in_fire       = _U8(0x27E8)   # fire/jump held (space/enter sources) [0bc6/58FC]
    in_aux        = _U8(0x27E9)   # the sixth flag (single scancode source 0x2840) — idle-gate input [5D50]
    in_up         = _U8(0x27EA)   # up held [4897]
    in_down       = _U8(0x27EB)   # down held [489B]
    in_right      = _U8(0x27EC)   # right held (drives facing +1 [58BF])
    in_left       = _U8(0x27ED)   # left held (drives facing -1 [58D9])


#: Back-compat alias — the class began as the collision island's globals and grew into the player's.
CollisionGlobals = PlayerGlobals


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
        return self._backend.rw(0x2D00 + self.level * 2)

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


# ---- the fixed slot LISTS (StructArrays: indexed named records instead of pointer walks). Attached to
# PlayerGlobals here because RenderSlot/ProjectileSlot are defined below it in the file. ----
PlayerGlobals.render_slots = StructArray(0x4F0A, 0x12, 116, RenderSlot)     # the on-screen records
#     (0x4F0A..0x5732, slot 1 = the player); 116 = the span the forward oracle masks
PlayerGlobals.projectiles = StructArray(0x4F2E, 0x12, 4, ProjectileSlot)    # the 4 thrown-weapon slots [627D]
PlayerGlobals.trail_ring_slots = StructArray(0x4F76, 0x12, 5, RenderSlot)   # the trail/dust ring (the cursor
#     g.trail_ring walks DOWN with wrap 0x4F76 -> 0x4FBE) [5E2E-5E37]
PlayerGlobals.effect_row = StructArray(0x56A2, 0x12, 8, RenderSlot)         # the 7585 effect/boss-health row
PlayerGlobals.wall_markers = StructArray(0x6EA9, 8, 20, WallMarker)         # the 64FA wall-impact list
PlayerGlobals.l6_projectiles = StructArray(0x7DAF, 0xB, 5, L6Projectile)    # the L6 tree-boss projectiles
PlayerGlobals.l6_render_slots = StructArray(0x55EE, 0x12, 5, RenderSlot)    # ...their projected render slots
PlayerGlobals.boss_targets = StructArray(0x5648, 0x12, 5, RenderSlot)       # the boss/camera-target records
#     (x/y/sprite per record; +5 = the flash flags byte; boss_x/boss_y alias record 0's x/y) [6E42/7113]
