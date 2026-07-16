"""The DETACHABLE bridge's half of the DOS byte layout — the verification-only serialisers.

**The layout itself moved to :mod:`pre2.native.graph_layout` on 2026-07-16** (Stage 2.5 of
docs/pre2/offset_free_release_plan.md). Reason: the object graph's offset-aware CONSTRUCTION has to be
reachable from shipped code, or the product can never build its own graph at boot (scripts/lint.py forbids
``pre2/native`` importing ``pre2.bridge`` — "the workbench is detachable; it imports the product, never the
reverse"). While it lived here, the object graph could only ever run inside verification scripts, and the
shipped default stayed the byte image no matter how many modules were converted.

What remains here is what genuinely belongs on the workbench: helpers that exist ONLY to serialise the model
to/from a DOS image for byte-exact verification, and :class:`NamedImageBackend`, which resolves a name-keyed
view against the image (the shipped ``NamedObjectBackend`` serves the object path). Everything else is
re-exported below so existing bridge/script/test importers keep working unchanged.
"""
from __future__ import annotations

from pre2.game.model import EffectSlot, Player, Rng
# The layout + graph construction (moved to the shipped side; re-exported here for existing importers).
from pre2.native.graph_layout import (  # noqa: F401
    ACTOR_BASE, ACTOR_COUNT, ACTOR_LAYOUT, ACTOR_STRIDE, ATTACK_STATE_LAYOUT, ATTRACT_STATE_LAYOUT,
    BONUS_CELL_LAYOUT, BOSS_LAYOUT, BOSS_SCRIPT_LAYOUT, CAMERA_LAYOUT, CAMERA_SCRIPT_LAYOUT, DGROUP_BASE,
    DIFFICULTY_MODE_LAYOUT, DataclassBackend, EFFECT_SLOT_LAYOUT, HIT_SCRATCH_LAYOUT, INPUT_LAYOUT,
    LEVEL_STATE_LAYOUT, MOTION_LAYOUT, PLAYER_BASE, PLAYER_LAYOUT, PLAYER_STATE_LAYOUT, PROGRESS_LAYOUT,
    RNG_LAYOUT, SCENERY_STATE_LAYOUT, SCROLL_LAYOUT, SPAWN_CURSOR_LAYOUT, WALL_MARKER_LAYOUT,
    _ARENA_HI, _ARENA_LO, _ARENA_REF_FIELDS, _ARENA_STRIDE_END, _ASSET_REF_FIELDS, _BUFFER_BYTES, _BUFFERS,
    _BURST_BASE, _BURST_COUNT, _DEBRIS_BASE, _DEBRIS_COUNT, _DST_BASE, _DST_COUNT, _EFFECT_ROW_BASE,
    _EFFECT_ROW_COUNT, _PROJECTILE_BASE, _PROJECTILE_COUNT, _REF_FIELDS, _RING_BASE, _RING_COUNT, _RNG_LCG,
    _ROR, _ROUTES, _RefSwizzle, _SLOT0_BASE, _SLOT0_COUNT, _SPARSE, _TARGET_BASE, _TARGET_COUNT,
    _arena_from_offset, _arena_to_offset, _obj_from_image, _obj_to_image, _rd, _wr,
)


def player_from_image(data) -> Player:
    """Deserialise the player object from the original byte image (bridge / verification only)."""
    data = getattr(data, "data", data)
    return Player(**{f: _rd(data, PLAYER_BASE, off, w, s) for f, off, w, s in PLAYER_LAYOUT})


def player_to_image(player: Player, data) -> None:
    """Serialise the player object back onto the original byte layout (bridge / verification only)."""
    data = getattr(data, "data", data)
    for f, off, w, _s in PLAYER_LAYOUT:
        _wr(data, PLAYER_BASE, off, w, getattr(player, f))


def rng_from_image(data) -> Rng:
    data = getattr(data, "data", data)
    return Rng(**{f: _rd(data, 0, off, w, s) for f, off, w, s in RNG_LAYOUT})


def rng_to_image(rng: Rng, data) -> None:
    data = getattr(data, "data", data)
    for f, off, w, _s in RNG_LAYOUT:
        _wr(data, 0, off, w, getattr(rng, f))


def globals_field_routing(image):
    """Build ``{PlayerGlobals-field-name: cluster-instance}`` for the scalar globals clusters (camera, motion,
    input, level_state, ..., the count==1 non-player/rng, non-EffectSlot routes), resolving each view field to
    the cluster instance that owns its OFFSET — so cluster-local dataclass name collisions (camera_script vs
    boss_script both have a ``script_ptr`` field, at different offsets) never mis-route. Bridge-only (uses
    offsets). Returns ``(instances, routing)`` — feed ``routing`` to NamedObjectBackend.register_fields to back
    the whole PlayerGlobals mega-view name-first."""
    from pre2.views.dgroup_view import PlayerGlobals
    data = getattr(image, "data", image)
    off_to_inst = {}
    instances = []
    for attr, cls, layout, base, count, stride in _ROUTES:
        if count != 1 or attr in ("player", "rng") or cls is EffectSlot:
            continue
        inst = _obj_from_image(cls, layout, data, base)
        instances.append(inst)
        for f, off, _w, _s in layout:
            off_to_inst[base + off] = (inst, f)
    routing = {}
    for name in dir(PlayerGlobals):
        desc = getattr(PlayerGlobals, name, None)
        off = getattr(desc, "off", None)
        if off is not None and off in off_to_inst:
            inst, dc_field = off_to_inst[off]
            if dc_field == name:                 # the cluster field must carry the SAME name the view uses
                routing[name] = inst
    return instances, routing


class NamedImageBackend:
    """Resolves a NAME-keyed view's fields (pre2/views/named_view) against the byte image via an offset layout.
    The DETACHABLE image resolver: it lets ONE name-keyed view definition serve the byte/verification path too
    (the shipped NamedObjectBackend serves the object path). It is verification-only, so it stays here in the
    bridge. ``layout`` is the same ``(name, rel_off, width, signed)`` list the serialiser uses."""

    __slots__ = ("_data", "_base", "_map")

    def __init__(self, image, base, layout):
        self._data = getattr(image, "data", image)
        self._base = base
        self._map = {f: (off, w, s) for f, off, w, s in layout}

    def read_field(self, view, name, width, signed):
        off, _w, _s = self._map[name]
        return _rd(self._data, self._base, off, width, signed)

    def write_field(self, view, name, width, v):
        off, _w, _s = self._map[name]
        _wr(self._data, self._base, off, width, v)
