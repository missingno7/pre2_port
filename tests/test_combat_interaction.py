"""Tests for the combat/pickup interaction island leaves (1030:8BF6, 8C13).

8BF6 (pack_spawn_pos) is shadow-verified live (scratchpad shadow_leaves.py: 1 witnessed hit in demo
105310, 0 mismatch). 8C13 (roll_bonus_sprite_id) is recovered from disasm and composes the already-VERIFIED
rng_lcg; it is not yet witnessed live, so these tests check the wrapper logic against rng_lcg directly.
"""
from __future__ import annotations

from pre2.recovered.combat_interaction import pack_spawn_pos, roll_bonus_sprite_id
from pre2.recovered.prng import rng_lcg


def test_pack_spawn_pos_scales_cells_by_16():
    # [di+3] word = x in low byte, y in high byte; each << 4 into the spawn globals
    assert pack_spawn_pos(0x0A05) == (0x05 << 4, 0x0A << 4)
    assert pack_spawn_pos(0xFF00) == (0x000, 0xFF0)
    assert pack_spawn_pos(0x00FF) == (0xFF0, 0x000)


def test_roll_bonus_sprite_id_in_range_and_advances_state():
    state = (0x11, 0x22, 0x33, 0x0044)
    sid, new_state = roll_bonus_sprite_id(state)
    assert 0x2080 <= sid <= 0x20DE                     # 0x2080 + v, v in [0, 0x5E]
    assert new_state != state                          # generator advanced at least once
    assert all(0 <= x <= 0xFF for x in new_state[:3]) and 0 <= new_state[3] <= 0xFFFF


def test_roll_bonus_sprite_id_matches_rng_lcg_rejection():
    # reproduce the rejection loop independently from the same seed and assert identical result + state
    a, b, c, d = (0x9A, 0x01, 0xC4, 0x1234)
    while True:
        a, b, c, d, ret = rng_lcg(a, b, c, d)
        v = ret & 0x7F
        if v < 0x5F:
            expect_id, expect_state = (0x2080 + v) & 0xFFFF, (a, b, c, d)
            break
    sid, new_state = roll_bonus_sprite_id((0x9A, 0x01, 0xC4, 0x1234))
    assert sid == expect_id
    assert new_state == expect_state
