"""The SHIPPED game's memory model — plain dataclasses, exactly how you'd write the game from scratch.

This is the north star of the object-model milestone: the released game runs on THESE objects — real Python
fields (``player.x``, ``rng.lcg_a``), no DGROUP offsets, no byte image. Nothing here knows the original DOS
memory layout; that knowledge lives entirely in the DETACHABLE bridge (``pre2/bridge/game_layout.py``), which
serialises these objects to/from the original byte image ONLY when verifying against the DOS original. Detach
the bridge and this is a clean, independent game (and, by construction, it can no longer replay or snapshot a
byte image — those are bridge/VM concepts).

Canonical fields only: where the ASM reads the same bytes at two widths (``sprite``'s high byte is ``flags``;
``facing``'s low byte is the anim-mirror flag; ``death_state`` aliases the generic slot's ``life``), the model
stores ONE field and exposes the alias as a derived property — a real object graph has no redundant storage.
The bridge's serialiser is what re-projects these onto the overlapping original bytes.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Player:
    """The player — position, kinematics, and animation state. No offsets; just the game's own fields."""

    x: int = 0            # world X, 12.4 fixed
    y: int = 0            # world Y, 12.4 fixed
    sprite: int = 0       # packed anim-frame word: (id & 0x1FFF) | flag bits
    xvel: int = 0         # X velocity, 12.4 fixed (signed)
    motion_mode: int = 0  # kinematics mode/shift (friction = 0xC >> mode)
    facing: int = 0       # +1 / -1 heading the FSM integrates with (signed)
    anim_b: int = 0       # anim B-state (anim-id memory; camera-shake gate input)
    anim_ptr: int = 0     # current anim-script cursor
    yvel: int = 0         # Y velocity, 12.4 fixed (signed)
    run_flag: int = 0     # run state (reset on an anim change)
    death_state: int = 0  # death/hurt state byte (0 = alive)

    @property
    def flags(self) -> int:
        """The sprite word's high byte (the DOS ``flags`` alias) — derived, not stored."""
        return (self.sprite >> 8) & 0xFF

    @property
    def anim_mirror(self) -> int:
        """The anim-mirror flag — ``facing``'s low byte (the DOS ``facing_lo`` alias) — derived."""
        return self.facing & 0xFF

    @property
    def alive(self) -> bool:
        return self.death_state == 0


@dataclass
class Rng:
    """The game's random-number generators — the 4-byte LCG mixer + the 1-word rotate generator."""

    lcg_a: int = 0
    lcg_b: int = 0
    lcg_c: int = 0
    lcg_d: int = 0
    ror: int = 0
