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

from pre2.game.model import Player, Rng

DGROUP_BASE = 0x1A0F << 4
PLAYER_BASE = 0x4F1C          # the player render/physics record base [asm]
_RNG_LCG = 0x2CEC             # the 4-byte LCG mixer
_ROR = 0x28C1                 # the 1-word rotate generator

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


class DataclassBackend:
    """Run the game with the PLAYER's live state as a real :class:`Player` dataclass, not bytes.

    A north-star step: ``NativeGameState.backend`` swaps to this and the gameplay tick runs unchanged, but every
    read/write to the player record (0x4F1C..0x4F2D) is routed — via the bridge layout — to/from the fields of a
    live ``Player`` object (``self.player.x``, ``.sprite``, ...). Everything else stays in the image. So the
    player is a genuine object graph node during the tick; the offsets live ONLY here in the bridge mapping, not
    in the game logic or the store. ``materialize`` folds the player object back to bytes for the digest.
    """

    _IS_DGROUP_BACKEND = True
    __slots__ = ("player", "_img", "_pmap")

    def __init__(self, seed):
        data = getattr(seed, "data", seed)
        self._img = data
        self.player = player_from_image(data)
        # player record byte offset (relative to PLAYER_BASE) -> (field, byte_index, width, signed)
        self._pmap: dict[int, tuple] = {}
        for f, off, w, s in PLAYER_LAYOUT:
            for k in range(w):
                self._pmap[off + k] = (f, k, w, s)

    def rb(self, off: int) -> int:
        off &= 0xFFFF
        m = self._pmap.get((off - PLAYER_BASE) & 0xFFFF)
        if m is None:
            return self._img[DGROUP_BASE + off]
        f, k, w, _s = m
        return (getattr(self.player, f) & ((1 << (8 * w)) - 1)) >> (8 * k) & 0xFF

    def wb(self, off: int, val: int) -> None:
        off &= 0xFFFF
        val &= 0xFF
        m = self._pmap.get((off - PLAYER_BASE) & 0xFFFF)
        if m is None:
            self._img[DGROUP_BASE + off] = val
            return
        f, k, w, s = m
        v = getattr(self.player, f) & ((1 << (8 * w)) - 1)
        v = (v & ~(0xFF << (8 * k))) | (val << (8 * k))
        if s and v & (1 << (8 * w - 1)):
            v -= 1 << (8 * w)
        setattr(self.player, f, v)

    def rw(self, off: int) -> int:
        return self.rb(off) | (self.rb((off + 1) & 0xFFFF) << 8)

    def ww(self, off: int, v: int) -> None:
        self.wb(off, v & 0xFF)
        self.wb((off + 1) & 0xFFFF, (v >> 8) & 0xFF)

    def materialize(self, data=None) -> None:
        player_to_image(self.player, self._img if data is None else data)
