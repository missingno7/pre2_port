"""Prehistorik 2 pseudo-random generators — recovered native logic (pure).

Two independent PRNGs live in the game-data segment and are sampled by the AI/effects code:

  * :func:`rng_ror` — ``1030:26CF``: a one-word rotate generator over ``[0x28C1]``.
  * :func:`rng_lcg` — ``1030:39DF``: a four-byte mixing generator over ``[0x2CEC..0x2CEF]``.

Both are pure state transforms here; the caller owns the words in memory and writes the new state back. Each
block is annotated with its ``[asm <offset>]`` origin and proven byte-exact in shadow against the ASM.
"""
from __future__ import annotations

__all__ = ["rng_ror", "rng_lcg", "RNG_ROR_STATE", "RNG_LCG_STATE"]

RNG_ROR_STATE = 0x28C1                 # the single 16-bit word of rng_ror's state
RNG_LCG_STATE = (0x2CEC, 0x2CED, 0x2CEE, 0x2CEF)   # a,b,c (bytes) + d (word at 0x2CEF) of rng_lcg


def _ror16(v: int, n: int) -> int:
    v &= 0xFFFF
    n &= 15
    return ((v >> n) | (v << (16 - n))) & 0xFFFF


def rng_ror(state: int) -> int:
    """Recover ``1030:26CF`` — advance the rotate-generator and return the new value (== the new state).

    ``new = ror16((state + 0x9248) & 0xFFFF, 3)``; the routine stores ``new`` back to ``[0x28C1]`` and returns
    it in ``AX`` (callers typically use ``AL``). ``state`` is the current ``[0x28C1]`` word."""
    return _ror16((state + 0x9248) & 0xFFFF, 3)            # [asm 26D2-26DB]


def rng_lcg(a: int, b: int, c: int, d: int) -> tuple[int, int, int, int, int]:
    """Recover ``1030:39DF`` — advance the four-byte mixing generator and return ``(a', b', c', d', ret)``.

    ``a``=``[0x2CEC]`` byte, ``b``=``[0x2CED]`` byte, ``c``=``[0x2CEE]`` byte, ``d``=``[0x2CEF]`` word. The
    routine returns ``AL = b'`` (the new ``[0x2CED]``). The caller writes the four new state values back."""
    d = (d + (a & 0xFF)) & 0xFFFF                          # [asm 39E8-39F3] d += zero-extended a
    a = (a + 3 + (d >> 8)) & 0xFF                          # [asm 39F7-39FE] a += 3 + high byte of d
    b = (((b + c) & 0xFF) * 2 + a) & 0xFF                  # [asm 3A01-3A0D] b = ((b+c)*2 + a)
    c = (c ^ a ^ b) & 0xFF                                 # [asm 3A11-3A19] c ^= a ^ b
    return a, b, c, d, b                                   # [asm 3A1D] returns the new b ([0x2CED])
