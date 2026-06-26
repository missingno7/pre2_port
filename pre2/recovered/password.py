"""Prehistorik 2 level-password generator — recovered native logic (pure).

Recovers the code generator at ``1030:932F`` (found via the "ENTER CODE" menu at 1030:9990, which reads 4 hex
chars into a 16-bit value and validates it by looping levels 0..N and comparing to ``932F(level)``):

    code(index) = rol16( ((index ^ 0x55A3) * seed) & 0xFFFF, rot )          [asm 939C..93A8]

``seed`` is a MACHINE fingerprint ([0xA333]) the game computes once from the BIOS ROM checksum (``F000:FFF0``
+ any video BIOS, ``932F`` first-call path 9343..9392); if that sum is 0 it falls back to ``0x20``. On the
DRM-free GOG build under the VM/DOSBox the BIOS region is zeroed, so the seed is the ``0x20`` fallback and the
codes are deterministic — which is why level 1 reads A305 (beginner) / A905 (expert). ``rot`` is ``cs:[5]``
(== 3 on this build). The codes are therefore BUILD/BIOS specific; pass a different ``seed`` for another BIOS.

Index → level/difficulty: index 0 == level 1 beginner and index 10 == level 1 expert (both VERIFIED), so
``index = (10 if expert else 0) + (level - 1)``. The validator accepts indices 0..0x12 (the 9A70 loop).
"""
from __future__ import annotations

__all__ = ["PASSWORD_XOR", "DEFAULT_SEED", "DEFAULT_ROT", "LEVELS_PER_MODE",
           "level_code", "password", "password_table"]

PASSWORD_XOR = 0x55A3     # [asm 939C xor ax,0x55a3]
DEFAULT_SEED = 0x20       # [asm 9390] the zeroed-BIOS fallback -> the seed on the GOG build under the VM/DOSBox
DEFAULT_ROT = 3           # cs:[5] rotate count (this build)
LEVELS_PER_MODE = 10      # beginner = indices 0..9, expert = indices 10..19 (index 10 == L1 expert, verified)
_MAX_VALID_INDEX = 0x12   # [asm 9A7C cmp dx,0x12] the validator's level loop upper bound


def _rol16(v: int, c: int) -> int:
    c &= 15
    v &= 0xFFFF
    return ((v << c) | (v >> (16 - c))) & 0xFFFF if c else v


def level_code(index: int, seed: int = DEFAULT_SEED, rot: int = DEFAULT_ROT) -> int:
    """The 16-bit password value for level ``index`` — recovers ``1030:932F``'s output
    (``rol16((index ^ 0x55A3) * seed, rot)``). Render as 4 upper-hex digits for the on-screen code."""
    return _rol16(((index ^ PASSWORD_XOR) * seed) & 0xFFFF, rot)


def password(level: int, expert: bool = False, seed: int = DEFAULT_SEED, rot: int = DEFAULT_ROT) -> str:
    """The 4-hex-char password string for ``level`` (1-based) in beginner or expert mode."""
    index = (LEVELS_PER_MODE if expert else 0) + (level - 1)
    return f"{level_code(index, seed, rot):04X}"


def password_table(seed: int = DEFAULT_SEED, rot: int = DEFAULT_ROT) -> list[tuple[int, str, str]]:
    """``[(level, beginner_code, expert_code), ...]`` for levels 1..LEVELS_PER_MODE."""
    return [(lvl, password(lvl, False, seed, rot), password(lvl, True, seed, rot))
            for lvl in range(1, LEVELS_PER_MODE + 1)]


if __name__ == "__main__":   # quick CLI: print the table for this build
    print(f"PRE2 level passwords (seed={DEFAULT_SEED:#04x}, rot={DEFAULT_ROT}):")
    print("  level  beginner  expert")
    for lvl, beg, exp in password_table():
        print(f"   L{lvl:<2}    {beg}      {exp}")
