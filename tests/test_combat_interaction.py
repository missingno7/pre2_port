"""Tests for the combat/pickup interaction island leaves (1030:8BF6, 8C13).

8BF6 (pack_spawn_pos) is shadow-verified live (scratchpad shadow_leaves.py: 1 witnessed hit in demo
105310, 0 mismatch). 8C13 (roll_bonus_sprite_id) is recovered from disasm and composes the already-VERIFIED
rng_lcg; it is not yet witnessed live, so these tests check the wrapper logic against rng_lcg directly.
"""
from __future__ import annotations

from pre2.recovered.combat_interaction import (
    BURST_SPRITE,
    HALF_LO,
    HALF_WX,
    PASS_FLAG,
    SCORE_LO,
    SPAWN_X,
    SPAWN_Y,
    SPAWNED_PTR,
    hitbox_overlap,
    pack_spawn_pos,
    roll_bonus_sprite_id,
    spawn_debris_element,
    spawn_effect_burst,
)
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


# ---- hitbox_overlap (8D7B) — shadow-verified byte-exact over 1895 live calls / 6 demos ----
def _hb_mem(src, tgt, *, x_half=0x10, y_half=0x10, x_width=0x08, a312=0, f2a=0):
    """Two sprite records (id 0 -> table index 0) at 0x100/0x200 plus the half-extent tables."""
    kv = {0x100: src[0], 0x102: src[1], 0x104: src[2],
          0x200: tgt[0], 0x202: tgt[1], 0x204: tgt[2],
          HALF_LO: x_half, HALF_LO + 1: y_half, HALF_WX: x_width,
          PASS_FLAG: a312, 0x4F2A: f2a}
    rb = lambda o: kv.get(o, 0) & 0xFF
    rw = lambda o: kv.get(o, 0) & 0xFFFF
    return rb, rw


def test_hitbox_coincident_overlaps():
    rb, rw = _hb_mem((0x100, 0x100, 0), (0x100, 0x100, 0))
    hit, writes = hitbox_overlap(rb, rw, 0x100, 0x200)
    assert hit is True
    assert writes[0xA330] == (0, 1)            # depth > y_half>>1 here -> detail not set


def test_hitbox_far_apart_culled_by_coarse_gate():
    rb, rw = _hb_mem((0x100, 0x100, 0), (0x200, 0x100, 0))   # |dX| = 0x100 >= 0x40
    hit, writes = hitbox_overlap(rb, rw, 0x100, 0x200)
    assert hit is False
    assert writes == {0xA330: (0, 1)}


def test_hitbox_far_y_culled():
    rb, rw = _hb_mem((0x100, 0x100, 0), (0x100, 0x180, 0))   # |dY| = 0x80 >= 0x46
    hit, _ = hitbox_overlap(rb, rw, 0x100, 0x200)
    assert hit is False


def test_hitbox_sets_vertical_detail_when_shallow():
    # dY = 0x0A, y_half = 0x10 -> depth = 6 <= y_half>>1 (8) -> detail set (si != player)
    rb, rw = _hb_mem((0x100, 0x100, 0), (0x100, 0x10A, 0))
    hit, writes = hitbox_overlap(rb, rw, 0x100, 0x200)
    assert writes[0xA330] == (1, 1)
    assert writes[0xA331] == (0x06, 2)


# ---- spawn_effect_burst (8D1B) — shadow byte-exact (6-spawn burst in demo 140619) ----
def test_spawn_effect_burst_alternates_velocity_into_free_slots():
    LO = 0x50A8
    kv = {LO + 4: 0xFFFF, LO + 0x12 + 4: 0xFFFF,        # two free slots
          BURST_SPRITE: 0x2046, SPAWN_X: 0x140, SPAWN_Y: 0x80}
    rb = lambda o: kv.get(o, 0) & 0xFF
    rw = lambda o: kv.get(o, 0) & 0xFFFF
    w = spawn_effect_burst(rb, rw, 0x20, 0x10, 2)
    assert w[LO + 4] == (0x2046, 2) and w[LO] == (0x140, 2) and w[LO + 2] == (0x80, 2)
    assert w[LO + 6] == (0x20, 2)                        # slot0 Xvel = ax
    assert w[LO + 0x12 + 6] == ((-0x20) & 0xFFFF, 2)     # slot1 Xvel = negated ax
    assert w[LO + 0x12 + 0xE] == (0x10, 2)               # slot1 Yvel = dx (step-down applies after)


# ---- spawn_debris_element (8875) — shadow byte-exact (7 kills) ----
def test_spawn_debris_element_fills_pool_and_bumps_score():
    POOL = 0x5450
    # sprite 0x4C -> bx=2 -> score word at (4 - 0x5CAD) & 0xFFFF
    score_addr = (4 - 0x5CAD) & 0xFFFF
    kv = {POOL + 4: 0xFFFF,                              # pool slot 0 free
          0x300: 0x111, 0x302: 0x222,                   # position source (si, non-effect)
          score_addr: 0x0100, SCORE_LO: 0, SCORE_LO + 2: 0}
    rb = lambda o: kv.get(o, 0) & 0xFF
    rw = lambda o: kv.get(o, 0) & 0xFFFF
    w, slot = spawn_debris_element(rb, rw, 0x4C, 0x300)
    assert slot == POOL
    assert w[POOL + 4] == (0x4C, 2) and w[POOL] == (0x111, 2) and w[POOL + 2] == (0x222, 2)
    assert w[POOL + 0xC] == (0x2C, 2)
    assert w[SPAWNED_PTR] == (POOL, 2)
    assert w[SCORE_LO] == (0x0100, 2) and w[SCORE_LO + 2] == (0, 2)
