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
    """The whole game state as a graph: named-field nodes + the named arenas. No byte image."""

    nodes: dict[str, Node]
    arenas: dict[str, bytes]

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
    arenas = {name: bytes(data[DGROUP_BASE + lo:DGROUP_BASE + hi + 1]) for name, (lo, hi) in ARENAS.items()}
    return GameState(nodes=nodes, arenas=arenas)


def to_image(gs: GameState, data) -> None:
    """Write the object graph back over a DGROUP image (the bridge serialiser) — the named region becomes
    byte-identical to the image ``gs`` was read from. Only the named region is touched."""
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
    for name, (lo, hi) in ARENAS.items():
        blob = gs.arenas[name]
        if len(blob) != hi - lo + 1:
            raise ValueError(f"arena {name!r}: expected {hi - lo + 1} bytes, got {len(blob)}")
        data[DGROUP_BASE + lo:DGROUP_BASE + hi + 1] = blob
