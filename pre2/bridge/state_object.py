"""The game state as an OBJECT GRAPH — and its bit-exact serialiser to/from a DGROUP image.

This is Phase 1 of the object-model milestone: prove that the whole named game state is losslessly a graph of
typed nodes (not a byte image), by round-tripping ``image -> GameState -> image`` byte-identically over the
whole tick corpus. The graph mirrors the shipped views' grouping — one node per view class (``PlayerGlobals``
is the canonical globals; ``CollisionGlobals`` is its alias over the same offsets), plus the small structured
record views (``PlayerView``/``RngView``/``ScrollScriptView``/``ProximityView``/``SwarmView``/``LightFadeView``/
``LoaderGlobals``) and the variable-stride ``entity_arena``.

The SERIALISER (``from_image`` / ``to_image``) is the bridge's half — it speaks offsets and the original DGROUP
format, so it lives here in ``pre2.bridge`` (detachable). The GRAPH itself (``GameState`` / ``Node``) carries no
offset — it is the shape a shipped product would run on. Round-trip identity is proven by
``scripts/verify_object_roundtrip.py``; it is the foundation the later phases (structuring the arena into record
objects, running the tick on the graph, dissolving the offsets out of the game code) build on.
"""
from __future__ import annotations

from dataclasses import dataclass

from pre2.bridge.field_registry import ARENAS, FIELDS

DGROUP_BASE = 0x1A0F << 4

_ENTITY_ARENA_KEY = next(iter(ARENAS))          # the sole variable-stride arena in the registry
_ENTITY_STRIDE_END = 0x32                        # a stride byte >= this terminates the walk [asm 6916]


@dataclass
class EntityRecord:
    """One record of the 2nd-pass entity list — the variable-stride linked-list entry the tick walks
    (``second_pass_tick``). ``data`` is the record's exact ``stride`` bytes; the typed properties name the
    known header fields (``[+0]`` stride, ``[+1]`` flags/handler, ``[+2]`` sprite-ref word, ``[+4]`` skip
    flags). The body past the header is handler-specific and stays bytes until each handler's layout is
    mapped — but the record is now an OBJECT in a list, not an anonymous slice of a blob."""

    data: bytes

    @property
    def stride(self) -> int:
        return self.data[0]

    @property
    def flags1(self) -> int:
        return self.data[1]

    @property
    def handler_idx(self) -> int:
        return self.data[1] & 0x7F

    @property
    def off_screen_cull(self) -> bool:
        return bool(self.data[1] & 0x80)

    @property
    def sprite_ref(self) -> int:
        return self.data[2] | (self.data[3] << 8)

    @property
    def empty(self) -> bool:
        return self.sprite_ref == 0xFFFF

    @property
    def skip(self) -> int:
        return self.data[4]


@dataclass
class EntityArena:
    """The 2nd-pass entity list as a LIST of records (entry 0 = the player) + the tail — the stale bytes from
    the terminator to the arena end, preserved verbatim so the region round-trips byte-exact. Replaces the
    opaque byte blob: the arena is now a structured object graph node."""

    records: list[EntityRecord]
    tail: bytes

    def to_bytes(self) -> bytes:
        return b"".join(r.data for r in self.records) + self.tail


def _parse_entity_arena(blob: bytes) -> EntityArena:
    """Walk the variable-stride list exactly as the tick does: each record is ``stride`` bytes; a stride byte
    ``>= 0x32`` (or 0, or an overrun) terminates. Everything from there is the tail."""
    records: list[EntityRecord] = []
    i, n = 0, len(blob)
    while i < n:
        stride = blob[i]
        if stride == 0 or stride >= _ENTITY_STRIDE_END or i + stride > n:
            break
        records.append(EntityRecord(bytes(blob[i:i + stride])))
        i += stride
    return EntityArena(records=records, tail=bytes(blob[i:]))

# CollisionGlobals declares the same offset set as PlayerGlobals (verified identical) — one graph node, two
# view names. Merge it so the graph has a single canonical globals node rather than a duplicated one.
_ALIASED_NODES = {"CollisionGlobals": "PlayerGlobals"}


def _node_specs() -> dict[str, dict[str, tuple[int, int]]]:
    """view-class node -> {field_name: (dgroup_offset, width)}, from the machine-generated registry."""
    specs: dict[str, dict[str, tuple[int, int]]] = {}
    for name, (off, width) in FIELDS.items():
        cls, field = name.split(".", 1)
        cls = _ALIASED_NODES.get(cls, cls)
        specs.setdefault(cls, {})[field] = (off, width)
    return specs


class Node:
    """One named-field group in the state graph — attribute access over its fields (``gs.player.x``). Carries no
    offset; it is pure named data. (Aliased fields — two names over one byte — hold the same value by
    construction, so writing them back in any order rebuilds the image identically.)"""

    __slots__ = ("_v",)

    def __init__(self, values: dict[str, int]):
        object.__setattr__(self, "_v", values)

    def __getattr__(self, name: str) -> int:
        try:
            return object.__getattribute__(self, "_v")[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, name: str, value: int) -> None:
        object.__getattribute__(self, "_v")[name] = value

    def fields(self) -> dict[str, int]:
        return object.__getattribute__(self, "_v")


@dataclass
class GameState:
    """The whole game state as a self-contained graph: named-field nodes + the STRUCTURED entity arena + the
    ``residue`` — the rest of the DGROUP heap (the per-level loaded tables: tile props, the anim/attack/camera
    script bytecode, the bonus-cell + effect-source lists, ...). The nodes + arena ARE the object graph; the
    residue is loaded INPUT data carried alongside as bytes (not dissolved into fields — it is bytecode and
    lookup tables, not game-logic state), so :func:`to_image` can reconstruct the whole DGROUP from a
    ``GameState`` ALONE, with no external byte image. Modeling residue regions into named loaded-table objects
    is the remaining incremental work; carrying it makes the graph the complete state of record today."""

    nodes: dict[str, Node]
    entity_arena: EntityArena
    residue: bytes = b""

    # convenience accessors for the structured record nodes (the interesting, non-globals ones)
    @property
    def globals(self) -> Node:
        return self.nodes["PlayerGlobals"]

    @property
    def player(self) -> Node:
        return self.nodes["PlayerView"]

    @property
    def rng(self) -> Node:
        return self.nodes["RngView"]

    @property
    def entities(self) -> list[EntityRecord]:
        """The live 2nd-pass entity records (entry 0 = the player)."""
        return self.entity_arena.records


def from_image(data) -> GameState:
    """Read a live DGROUP image into the object graph (the bridge deserialiser)."""
    data = getattr(data, "data", data)
    nodes: dict[str, Node] = {}
    for cls, fields in _node_specs().items():
        values: dict[str, int] = {}
        for field, (off, width) in fields.items():
            b = DGROUP_BASE + off
            values[field] = data[b] if width == 1 else data[b] | (data[b + 1] << 8)
        nodes[cls] = Node(values)
    lo, hi = ARENAS[_ENTITY_ARENA_KEY]
    entity_arena = _parse_entity_arena(bytes(data[DGROUP_BASE + lo:DGROUP_BASE + hi + 1]))
    residue = bytes(data[DGROUP_BASE:DGROUP_BASE + 0x10000])
    return GameState(nodes=nodes, entity_arena=entity_arena, residue=residue)


def to_image(gs: GameState, data=None):
    """Serialise the object graph back to a DGROUP image (the bridge serialiser). With ``data`` given, overlay
    the named region + arena onto it (byte-identical to the image ``gs`` was read from; only the named region
    is touched). With ``data=None``, reconstruct the WHOLE DGROUP from ``gs`` ALONE — seed from ``gs.residue``,
    then overlay the graph — proving the graph is a self-contained state of record with no external image.
    Returns the image written."""
    if data is None:
        if len(gs.residue) != 0x10000:
            raise ValueError(f"self-contained to_image needs a full DGROUP residue, got {len(gs.residue)} bytes")
        data = bytearray(DGROUP_BASE + 0x10000)
        data[DGROUP_BASE:DGROUP_BASE + 0x10000] = gs.residue
    data = getattr(data, "data", data)
    specs = _node_specs()
    for cls, node in gs.nodes.items():
        fields = specs[cls]
        vals = node.fields()
        for field, (off, width) in fields.items():
            v = vals[field]
            b = DGROUP_BASE + off
            data[b] = v & 0xFF
            if width == 2:
                data[b + 1] = (v >> 8) & 0xFF
    lo, hi = ARENAS[_ENTITY_ARENA_KEY]
    blob = gs.entity_arena.to_bytes()
    if len(blob) != hi - lo + 1:
        raise ValueError(f"entity arena: expected {hi - lo + 1} bytes, got {len(blob)}")
    data[DGROUP_BASE + lo:DGROUP_BASE + hi + 1] = blob
    return data


def _owner_map() -> dict[int, str]:
    """Every named DGROUP offset -> the graph node that owns it ('arena' for the entity arena)."""
    owner: dict[int, str] = {}
    for cls, fields in _node_specs().items():
        for off, width in fields.values():
            for k in range(width):
                owner[(off + k) & 0xFFFF] = cls
    lo, hi = ARENAS[_ENTITY_ARENA_KEY]
    for o in range(lo, hi + 1):
        owner[o] = "arena"
    return owner


class ObjectGraphBackend:
    """The game running ON the object graph: named state lives in per-node byte buckets (grouped by the
    graph's nodes) plus the entity-arena bucket; read-only residue stays in an image. Presents the same
    offset-keyed rb/rw/wb/ww API as the other backends, so ``NativeGameState.backend`` swaps to it with
    nothing else changing — proving the structured graph is a sufficient LIVE state of record, not just a
    snapshot. ``graph()`` reifies the current buckets as a :class:`GameState` (typed nodes + record list);
    ``materialize`` folds the buckets back over an image for the renderer / the byte-exact digest."""

    _IS_DGROUP_BACKEND = True
    __slots__ = ("_buckets", "_owner", "_img")

    def __init__(self, seed, residue_image=None):
        data = getattr(seed, "data", seed)
        self._owner = _owner_map()
        self._buckets: dict[str, dict[int, int]] = {}
        for off, node in self._owner.items():
            self._buckets.setdefault(node, {})[off] = data[DGROUP_BASE + off]
        self._img = data if residue_image is None else residue_image

    def rb(self, off: int) -> int:
        off &= 0xFFFF
        node = self._owner.get(off)
        if node is None:
            return self._img[DGROUP_BASE + off]
        return self._buckets[node][off]

    def wb(self, off: int, v: int) -> None:
        off &= 0xFFFF
        v &= 0xFF
        node = self._owner.get(off)
        if node is None:
            self._img[DGROUP_BASE + off] = v
        else:
            self._buckets[node][off] = v

    def rw(self, off: int) -> int:
        return self.rb(off) | (self.rb((off + 1) & 0xFFFF) << 8)

    def ww(self, off: int, v: int) -> None:
        self.wb(off, v & 0xFF)
        self.wb((off + 1) & 0xFFFF, (v >> 8) & 0xFF)

    def materialize(self, data=None) -> None:
        data = self._img if data is None else data
        for bucket in self._buckets.values():
            for off, v in bucket.items():
                data[DGROUP_BASE + off] = v

    def graph(self) -> GameState:
        """Reify the current live buckets as a typed object graph (nodes + entity record list)."""
        scratch = bytearray(len(self._img))
        self.materialize(scratch)
        return from_image(scratch)
