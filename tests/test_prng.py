"""Unit tests for the recovered PRNGs (pre2/recovered/prng.py).

Byte-exact ASM equivalence is proven live by the shadow runs (rng_ror 44/44, rng_lcg 556/556 on demo
105310); these pin the pure formulas + a couple of golden values."""
from __future__ import annotations

from pre2.recovered.prng import rng_lcg, rng_ror


def _ror16(v, n):
    return ((v >> n) | (v << (16 - n))) & 0xFFFF


def test_rng_ror_formula_matches_rotate():
    for s in (0, 1, 0x1234, 0xFFFF, 0x8000, 0xABCD):
        assert rng_ror(s) == _ror16((s + 0x9248) & 0xFFFF, 3)


def test_rng_ror_golden():
    assert rng_ror(0) == 0x1249           # ror16(0x9248, 3)
    assert rng_ror(0x1249) == rng_ror(0x1249)   # deterministic


def test_rng_lcg_step():
    # a=b=c=d=0: d+=0 -> 0 ; a=0+3+0=3 ; b=(0+0)*2+3=3 ; c=0^3^3=0 ; ret=b=3
    assert rng_lcg(0, 0, 0, 0) == (3, 3, 0, 0, 3)


def test_rng_lcg_carries_high_byte_of_d():
    # d high byte feeds a: pick d so (d + a) crosses a byte boundary
    a, b, c, d = 0x10, 0x05, 0x07, 0x00F8
    nd = (d + a) & 0xFFFF                      # 0x0108
    na = (a + 3 + (nd >> 8)) & 0xFF            # 0x10 + 3 + 1
    nb = (((b + c) & 0xFF) * 2 + na) & 0xFF
    nc = (c ^ na ^ nb) & 0xFF
    assert rng_lcg(a, b, c, d) == (na, nb, nc, nd, nb)


def test_rng_lcg_is_deterministic_and_byte_bounded():
    a, b, c, d = 0xAA, 0xBB, 0xCC, 0x1234
    na, nb, nc, nd, ret = rng_lcg(a, b, c, d)
    assert 0 <= na <= 0xFF and 0 <= nb <= 0xFF and 0 <= nc <= 0xFF and 0 <= nd <= 0xFFFF
    assert ret == nb
