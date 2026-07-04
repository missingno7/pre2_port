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
           "bios_seed", "level_code", "password", "password_table", "validate_code",
           "password_cheat_hash", "is_cheat_sequence", "warp_index_to_level"]

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


def bios_seed(mem) -> int:
    """[asm 932F 9343..9390] The one-time BIOS-ROM checksum that becomes ``[0xA333]`` (the password/decor seed).

    ``mem`` is a 1 MB-addressable byte sequence (``mem[(seg << 4) + off]``). Sums the BIOS date bytes
    ``F000:FFF0..FFFF`` (dl += b / dh sbb b) then every option/video ROM ``C000..EFFF`` that carries the
    ``0xAA55`` signature (dl += b / dh xor b, 0x80 bytes, advancing by the ROM's declared size), and returns
    the 16-bit ``dx``. When the total is 0 — as on the zeroed-BIOS GOG/DOSBox build — it falls back to ``0x20``
    (``mov dl,0x20``), which is why this build's codes are the deterministic A305/A905 (see the module header)."""
    dl = dh = 0
    for si in range(0xFFF0, 0x10000):                        # [asm 934d] F000:FFF0..FFFF (16 date bytes)
        b = mem[0xF0000 + si]
        s = dl + b; cf = s >> 8; dl = s & 0xFF               # [asm 934d] add dl, es:[si]
        dh = (dh - b - cf) & 0xFF                            # [asm 9350] sbb dh, es:[si]
    ax = 0xC000                                              # [asm 9356] scan the option ROMs C000..EFFF
    while (ax >> 8) < 0xF0:                                  # [asm 9387/938a] cmp ah,0xf0 / jb
        base = (ax & 0xFFFF) << 4
        if (mem[base] | (mem[base + 1] << 8)) == 0xAA55:     # [asm 935b] the ROM signature
            for si in range(0x80):                           # [asm 9366-9370] sum 0x80 bytes
                b = mem[base + si]
                s = dl + b; cf = s >> 8; dl = s & 0xFF       # [asm 9369] add dl, es:[si]
                dh = (dh ^ b) & 0xFF                         # [asm 936c] xor dh, es:[si]
            bh = mem[base + 2]                               # [asm 9372] ROM size in 512-byte blocks
            ax = (ax + ((bh << 8) >> 3)) & 0xFFFF            # [asm 9377-9384] advance ax by the ROM size (net)
        else:
            ax = (ax + 0x400) & 0xFFFF                       # [asm 9384] add ah,4 -> next 16 KB
    dx = (dh << 8) | dl
    return dx if dx != 0 else 0x20                           # [asm 938c-9390] or dx,dx / je -> mov dl,0x20


def password(level: int, expert: bool = False, seed: int = DEFAULT_SEED, rot: int = DEFAULT_ROT) -> str:
    """The 4-hex-char password string for ``level`` (1-based) in beginner or expert mode."""
    index = (LEVELS_PER_MODE if expert else 0) + (level - 1)
    return f"{level_code(index, seed, rot):04X}"


def validate_code(entered: int, seed: int = DEFAULT_SEED, rot: int = DEFAULT_ROT) -> tuple[int, bool] | None:
    """[asm 9A6E-9AAA] Validate an entered 16-bit ``ENTER CODE`` value against the level passwords.

    Scans indices ``0..0x12`` for ``level_code(index) == entered``; on a match returns ``(level, expert)`` where
    ``level`` is the 0-based level the game stores in ``[0x2D8A]`` and ``expert`` is ``index >= LEVELS_PER_MODE``
    (the ASM subtracts ``0xA`` and sets ``[0xB197]=1`` for an expert code). Returns ``None`` if no level matches
    (the menu then rotates the seed history and stays on the ENTER-CODE screen)."""
    for index in range(_MAX_VALID_INDEX + 1):              # [asm 9A70-9A7F] dx = 0..0x12
        if level_code(index, seed, rot) == entered:        # [asm 9A72-9A79] cmp [0xB1B9], 932F(dx)
            expert = index >= LEVELS_PER_MODE              # [asm 9A9D] cmp dl,0xa; jb
            return (index - LEVELS_PER_MODE if expert else index), expert   # [asm 9AA2/9AA6] [0x2D8A]=dl
    return None


#: [asm 9A53/9A58/9A5E] the level-warp cheat hash — the values ``password_cheat_hash`` must return for the three
#: prior 4-hex groups to be the magic "DEAD C0DE F00D" sequence. Stored as a HASH (not the literals) so the code
#: DEAD/C0DE/F00D can't be found by scanning the binary for constants.
_CHEAT_HASH = (0x36C8, 0x8BD1, 0x8E71)


def _rol(v: int, c: int, bits: int) -> int:
    c %= bits; m = (1 << bits) - 1; v &= m
    return ((v << c) | (v >> (bits - c))) & m if c else v


def password_cheat_hash(di: int, si: int, bp: int) -> tuple[int, int, int]:
    """[asm 9A3A-9A52] The obfuscated hash of the three prior ENTER-CODE groups (di=[0xB1B3], si=[0xB1B5],
    bp=[0xB1B7]). Returns ``(ax, bx, dx)``; the sequence is the level-warp cheat iff it equals ``_CHEAT_HASH``
    (which ``DEAD C0DE F00D`` produces — VERIFIED)."""
    ax = _rol(di, 2, 16)                                   # [asm 9A3C-9A3E] rol ax,1 x2
    ax = ((ax & 0xFF) * ((ax >> 8) & 0xFF)) & 0xFFFF       # [asm 9A40] mul ah -> AX = AL*AH
    ax = (ax ^ si) & 0xFFFF                                # [asm 9A42] xor ax, si
    prod = (ax * bp) & 0xFFFFFFFF                          # [asm 9A44] mul bp -> DX:AX
    ax = prod & 0xFFFF; dx = (prod >> 16) & 0xFFFF
    bx = (ax + si) & 0xFFFF                                # [asm 9A46-9A48] bx=ax; add bx,si
    bx = _rol(bx, 15, 16)                                  # [asm 9A4A] ror bx,1
    bl = _rol(bx & 0xFF, 1, 8) ^ ((bx >> 8) & 0xFF)        # [asm 9A4C-9A4E] rol bl,1; xor bl,bh
    bx = (((bx >> 8) & 0xFF) << 8) | (bl & 0xFF)
    bx = (bx ^ bp) & 0xFFFF                                # [asm 9A50] xor bx, bp
    return ax, bx, dx


def is_cheat_sequence(di: int, si: int, bp: int) -> bool:
    """True iff the three prior groups are the ``DEAD C0DE F00D`` level-warp cheat [asm 9A53-9A62]."""
    return password_cheat_hash(di, si, bp) == _CHEAT_HASH


def warp_index_to_level(index: int) -> tuple[int, bool]:
    """[asm 9A9B-9AAA] Map a warp index (level-number-minus-1, or a matched 0..0x12 loop index) to the game's
    ``([0x2D8A], expert)``: indices >= ``LEVELS_PER_MODE`` are expert (``[0x2D8A]`` = index - 10)."""
    expert = index >= LEVELS_PER_MODE
    return (index - LEVELS_PER_MODE if expert else index), expert


def password_table(seed: int = DEFAULT_SEED, rot: int = DEFAULT_ROT) -> list[tuple[int, str, str]]:
    """``[(level, beginner_code, expert_code), ...]`` for levels 1..LEVELS_PER_MODE."""
    return [(lvl, password(lvl, False, seed, rot), password(lvl, True, seed, rot))
            for lvl in range(1, LEVELS_PER_MODE + 1)]


if __name__ == "__main__":   # quick CLI: print the table for this build
    print(f"PRE2 level passwords (seed={DEFAULT_SEED:#04x}, rot={DEFAULT_ROT}):")
    print("  level  beginner  expert")
    for lvl, beg, exp in password_table():
        print(f"   L{lvl:<2}    {beg}      {exp}")
