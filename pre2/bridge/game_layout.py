"""The DETACHABLE bridge: the ONLY place that knows the original DOS byte layout of the game model.

``pre2/game/model.py`` is the clean, offset-free game (the shipped product). This module is the umbilical cord:
it maps those dataclasses to/from the original DGROUP byte image so the model can be verified byte-for-byte
against the DOS original. Ship without this module and the game has no notion of offsets, no byte image, and
therefore no replay/snapshot — it is just the object model.

A layout is ``(field, rel_offset, width, signed)`` per canonical field; the alias bytes (``flags``,
``facing_lo``, ``life``) are re-projections of a canonical field's bytes, so writing the canonical fields
reproduces them exactly — no separate entries needed. Evidence for each offset lives with the ``dgroup_view``
descriptors (the recovery spec); this table is the machine-readable serialisation layout.
"""
from __future__ import annotations

from pre2.game.model import Camera, Player, Progress, Rng

DGROUP_BASE = 0x1A0F << 4
PLAYER_BASE = 0x4F1C          # the player render/physics record base [asm]
_RNG_LCG = 0x2CEC             # the 4-byte LCG mixer
_ROR = 0x28C1                 # the 1-word rotate generator

# absolute-offset layouts (field, dgroup offset, width, signed) — the scattered original homes of the objects'
# fields. This mapping is the ONLY place the offsets live; the game model (pre2/game/model.py) has none.
CAMERA_LAYOUT = [
    ("col", 0x2DE4, 2, False), ("row", 0x2DE6, 2, False),
    ("fine_scroll", 0x6BC4, 1, False), ("row_factor", 0x6BF8, 2, False),
]
PROGRESS_LAYOUT = [
    ("score_lo", 0x6C0E, 2, False), ("score_hi", 0x6C10, 2, False),
    ("lives", 0x27D8, 1, False), ("energy", 0x27D6, 1, False), ("level", 0x2D8A, 1, False),
    ("bonus_letters", 0x6CA7, 1, False), ("utensils_mask", 0x6CA8, 1, False),
]

# (field, offset, width, signed). Player offsets are relative to PLAYER_BASE; Rng offsets are absolute DGROUP.
PLAYER_LAYOUT = [
    ("x", 0x00, 2, False), ("y", 0x02, 2, False), ("sprite", 0x04, 2, False),
    ("xvel", 0x06, 2, True), ("motion_mode", 0x08, 1, False), ("facing", 0x09, 2, True),
    ("anim_b", 0x0B, 1, False), ("anim_ptr", 0x0C, 2, False), ("yvel", 0x0E, 2, True),
    ("run_flag", 0x10, 1, False), ("death_state", 0x11, 1, False),
]
RNG_LAYOUT = [
    ("lcg_a", _RNG_LCG + 0, 1, False), ("lcg_b", _RNG_LCG + 1, 1, False),
    ("lcg_c", _RNG_LCG + 2, 1, False), ("lcg_d", _RNG_LCG + 3, 2, False),
    ("ror", _ROR, 2, False),
]


def _rd(data, base, off, width, signed):
    b = DGROUP_BASE + base + off
    v = data[b] if width == 1 else data[b] | (data[b + 1] << 8)
    if signed and v & (1 << (8 * width - 1)):
        v -= 1 << (8 * width)
    return v


def _wr(data, base, off, width, v):
    b = DGROUP_BASE + base + off
    v &= (1 << (8 * width)) - 1
    data[b] = v & 0xFF
    if width == 2:
        data[b + 1] = (v >> 8) & 0xFF


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


def _obj_from_image(cls, layout, data, base=0):
    data = getattr(data, "data", data)
    return cls(**{f: _rd(data, base, off, w, s) for f, off, w, s in layout})


def _obj_to_image(obj, layout, data, base=0):
    data = getattr(data, "data", data)
    for f, off, w, _s in layout:
        _wr(data, base, off, w, getattr(obj, f))


# the structures routed to live dataclasses: (attribute, class, layout, base). Everything else stays in the
# image. Grows toward the endpoint (whole tick on objects) one structure at a time.
_ROUTES = [
    ("player", Player, PLAYER_LAYOUT, PLAYER_BASE),
    ("rng", Rng, RNG_LAYOUT, 0),
    ("camera", Camera, CAMERA_LAYOUT, 0),
    ("progress", Progress, PROGRESS_LAYOUT, 0),
]


class DataclassBackend:
    """Run the game with named structures' live state as real offset-free dataclasses, not bytes.

    The north-star mechanism: ``NativeGameState.backend`` swaps to this and the gameplay tick runs UNCHANGED,
    but every read/write that lands in a routed structure's byte range is redirected — via the bridge layout —
    to/from the fields of a live dataclass (``self.player.x``, ``self.camera.col``, ``self.progress.lives``,
    ...). Everything else stays in the image. The offsets live ONLY here in the bridge mapping, not in the game
    logic or the store. Each structure added to ``_ROUTES`` shrinks the image's live surface. ``materialize``
    folds the objects back to bytes for the digest.
    """

    _IS_DGROUP_BACKEND = True
    __slots__ = ("_img", "_objs", "_map")

    def __init__(self, seed):
        data = getattr(seed, "data", seed)
        self._img = data
        self._objs: dict[str, object] = {}
        # absolute dgroup offset -> (attr, field, byte_index, width, signed)
        self._map: dict[int, tuple] = {}
        for attr, cls, layout, base in _ROUTES:
            self._objs[attr] = _obj_from_image(cls, layout, data, base)
            for f, off, w, s in layout:
                for k in range(w):
                    self._map[(base + off + k) & 0xFFFF] = (attr, f, k, w, s)

    # convenience accessors
    player = property(lambda self: self._objs["player"])
    rng = property(lambda self: self._objs["rng"])
    camera = property(lambda self: self._objs["camera"])
    progress = property(lambda self: self._objs["progress"])

    def rb(self, off: int) -> int:
        off &= 0xFFFF
        m = self._map.get(off)
        if m is None:
            return self._img[DGROUP_BASE + off]
        attr, f, k, w, _s = m
        return (getattr(self._objs[attr], f) & ((1 << (8 * w)) - 1)) >> (8 * k) & 0xFF

    def wb(self, off: int, val: int) -> None:
        off &= 0xFFFF
        val &= 0xFF
        m = self._map.get(off)
        if m is None:
            self._img[DGROUP_BASE + off] = val
            return
        attr, f, k, w, s = m
        v = getattr(self._objs[attr], f) & ((1 << (8 * w)) - 1)
        v = (v & ~(0xFF << (8 * k))) | (val << (8 * k))
        if s and v & (1 << (8 * w - 1)):
            v -= 1 << (8 * w)
        setattr(self._objs[attr], f, v)

    def rw(self, off: int) -> int:
        return self.rb(off) | (self.rb((off + 1) & 0xFFFF) << 8)

    def ww(self, off: int, v: int) -> None:
        self.wb(off, v & 0xFF)
        self.wb((off + 1) & 0xFFFF, (v >> 8) & 0xFF)

    def materialize(self, data=None) -> None:
        data = self._img if data is None else data
        for attr, _cls, layout, base in _ROUTES:
            _obj_to_image(self._objs[attr], layout, data, base)
