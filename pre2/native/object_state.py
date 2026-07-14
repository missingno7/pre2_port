"""The SHIPPED object-graph store — real named objects, addressed by name/reference, not DGROUP offsets.

``ObjectGraphStore`` holds the live game objects (``pre2.game.model`` dataclasses — ``Player``, ``Actor``,
``ArenaEntity``, ...) plus a byte-level compatibility map for the recovered logic not yet converted off
``rb``/``rw``. This module contains **zero DGROUP offset literals**: the map's integer keys are opaque IDs
handed to it by whoever constructs it — it does no address arithmetic of its own, just dict lookups and object
mutation. It also does not know how to (de)serialise to/from a DOS byte image — that needs the offset LAYOUT,
which lives in the detachable bridge (``pre2.bridge.game_layout``).

Reference-typed fields (``pre2.game.ref`` — ``ObjectRef``/``ArenaRef``/``AssetCursor``) may need translating
to/from the map's integer keyspace for legacy ``rb``/``rw`` callers; the bridge supplies an optional
``ref_swizzle`` object at construction to do that (dependency injection — this module never imports the bridge).

As gameplay logic converts to named/reference access (``entity.x`` instead of ``rb(si + 9)``), calls to
``rb``/``rw``/``wb``/``ww`` here should disappear; they exist only as a migration aid for not-yet-converted
callers and are not part of the intended end-state gameplay abstraction.
"""
from __future__ import annotations


class ObjectGraphStore:
    """A generic, offset-free object/map store: named objects (``self._objs``) plus an opaque byte-compat map
    (``self._map``: key -> ``(object, field, byte_index, width, signed)``). The BRIDGE builds both and hands them
    to this class's constructor — this class never computes a DGROUP address itself."""

    _IS_DGROUP_BACKEND = True
    __slots__ = ("_objs", "_map", "_level_data", "_readonly_image", "_ref_swizzle")

    def __init__(self, objs, map_, level_data, readonly_image=False, ref_swizzle=None):
        self._objs = objs
        self._map = map_
        self._level_data = level_data
        self._readonly_image = readonly_image
        # optional object (built by the bridge) that resolves reference-typed fields to/from the map's integer
        # keyspace for legacy rb/wb callers: is_ref_field(inst, field) / to_key(ref) / from_key(key). None means
        # "nothing in this map needs it."
        self._ref_swizzle = ref_swizzle

    # ---- named object access -------------------------------------------------------------------------------
    player = property(lambda self: self._objs["player"])
    rng = property(lambda self: self._objs["rng"])
    camera = property(lambda self: self._objs["camera"])
    progress = property(lambda self: self._objs["progress"])
    actors = property(lambda self: self._objs["actors"])
    entities = property(lambda self: self._objs["entities"])
    player_state = property(lambda self: self._objs["player_state"])
    spawn_cursor = property(lambda self: self._objs["spawn_cursor"])
    scenery_state = property(lambda self: self._objs["scenery_state"])
    boss = property(lambda self: self._objs["boss"])
    difficulty_mode = property(lambda self: self._objs["difficulty_mode"])
    level_state = property(lambda self: self._objs["level_state"])

    # ---- byte-compatibility shim (migration aid; not the gameplay abstraction) -----------------------------
    def rb(self, off: int) -> int:
        off &= 0xFFFF
        m = self._map.get(off)
        if m is None:                               # un-routed -> the read-only loaded tables
            return self._level_data[off]
        inst, f, k, w, _s = m
        sw = self._ref_swizzle
        if sw is not None and sw.is_ref_field(inst, f):
            return (sw.to_key(getattr(inst, f)) >> (8 * k)) & 0xFF
        if w == 0:                                  # a bytearray byte (record body / working buffer)
            return getattr(inst, f)[k]
        return (getattr(inst, f) & ((1 << (8 * w)) - 1)) >> (8 * k) & 0xFF

    def wb(self, off: int, val: int) -> None:
        off &= 0xFFFF
        val &= 0xFF
        m = self._map.get(off)
        if m is None:
            if self._readonly_image:
                raise AssertionError(f"gameplay tick wrote to the read-only loaded data at 0x{off:04X} — an "
                                     f"un-routed mutable byte (the object graph is not the complete store)")
            self._level_data[off] = val
            return
        inst, f, k, w, s = m
        sw = self._ref_swizzle
        if sw is not None and sw.is_ref_field(inst, f):
            cur = sw.to_key(getattr(inst, f))
            cur = (cur & ~(0xFF << (8 * k))) | (val << (8 * k))
            setattr(inst, f, sw.from_key(inst, f, cur & 0xFFFF))
            return
        if w == 0:                                  # a bytearray byte (record body / working buffer)
            getattr(inst, f)[k] = val
            return
        v = getattr(inst, f) & ((1 << (8 * w)) - 1)
        v = (v & ~(0xFF << (8 * k))) | (val << (8 * k))
        if s and v & (1 << (8 * w - 1)):
            v -= 1 << (8 * w)
        setattr(inst, f, v)

    def rw(self, off: int) -> int:
        return self.rb(off) | (self.rb((off + 1) & 0xFFFF) << 8)

    def ww(self, off: int, v: int) -> None:
        self.wb(off, v & 0xFF)
        self.wb((off + 1) & 0xFFFF, (v >> 8) & 0xFF)
