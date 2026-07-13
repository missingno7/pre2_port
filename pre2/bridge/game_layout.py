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
