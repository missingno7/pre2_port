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
    advance_death_anim,
    death_handler,
    hitbox_overlap,
    pack_spawn_pos,
    projectile_vs_enemies,
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


# ---- advance_death_anim (80CB) — shadow byte-exact (3 launch-path kills) ----
def test_advance_death_anim_jumps_past_marker():
    # [di+0xC] = 0x400; script words at 0x402/0x404 non-marker, 0x406 = 0x7D00 -> new ptr 0x408
    kv = {0x100 + 0xC: 0x400, 0x402: 0x1234, 0x404: 0x5678, 0x406: 0x7D00}
    rw = lambda o: kv.get(o, 0) & 0xFFFF
    assert advance_death_anim(rw, 0x100) == 0x408


# ---- death_handler (8C72) — shadow byte-exact on both paths (3 launch + 1 bonus kill) ----
def _byte_mem(words=None, byts=None):
    m = {}
    for o, v in (byts or {}).items():
        m[o & 0xFFFF] = v & 0xFF
    for o, v in (words or {}).items():
        m[o & 0xFFFF] = v & 0xFF
        m[(o + 1) & 0xFFFF] = (v >> 8) & 0xFF
    rb = lambda o: m.get(o & 0xFFFF, 0)
    rw = lambda o: rb(o) | (rb((o + 1) & 0xFFFF) << 8)
    return rb, rw


def test_death_handler_launch_path():
    DI, BX, SRC = 0x4FD0, 0x600, 0x4F0A
    cnt_addr = (0 - 0x5C0F) & 0xFFFF        # cnt_idx 0 -> count 0 (no debris loop)
    rb, rw = _byte_mem(
        words={DI + 0xC: 0x700, 0x702: 0x7D00},   # anim script: marker at 0x702 -> new ptr 0x704
        byts={DI + 0x10: 0, cnt_addr: 0, BX + 8: 0,
              BX + 4: 0x01,                  # [def+4] bit0 set -> launch path
              0x7B19: 0x10,                  # damage -> yvel = -0x10*8 = 0xFF80
              SRC + 5: 0x80})                # attacker facing bit set -> keep xvel sign
    b = death_handler(rb, rw, BX, DI, SRC)
    word = lambda o: b.get(o, 0) | (b.get(o + 1, 0) << 8)
    assert b[DI + 0xE] == 0xFF               # marked dead
    assert word(DI + 0xC) == 0x704           # anim advanced past the 0x7D00 marker
    assert word(DI + 0xA) == 0xFF80          # Yvel = -min(dmg,0x19)*8
    assert word(DI + 8) == 0xFFC0            # Xvel = sar(0xFF80,1), facing-bit set -> kept negative


def test_death_handler_bonus_path_marks_dead_and_sets_spawn_globals():
    DI, BX, SRC = 0x4FD0, 0x600, 0x4F0A
    cnt_addr = (0 - 0x5C0F) & 0xFFFF
    rb, rw = _byte_mem(
        words={DI: 0x123, DI + 2: 0x456, 0x50A8 + 4: 0xFFFF},  # enemy pos + one free effect slot
        byts={DI + 0x10: 0, cnt_addr: 0, BX + 8: 0, BX + 4: 0x00})  # bit0 clear -> bonus path
    b = death_handler(rb, rw, BX, DI, SRC)
    word = lambda o: b.get(o, 0) | (b.get(o + 1, 0) << 8)
    assert b[DI + 0xE] == 0xFF               # marked dead
    assert word(SPAWN_X) == 0x123 and word(SPAWN_Y) == 0x456   # [0xA336]/[0xA338] = enemy pos
    assert word(BURST_SPRITE) == 0x2046      # [0xA33A] = death-bonus sprite
    assert word(0x50A8 + 4) == 0x2046        # the free slot got a bonus sprite


# ---- projectile_vs_enemies (8C21) — shadow byte-exact (170 calls / 5 demos, incl. 4 kills) ----
def test_projectile_no_hit_when_all_slots_empty():
    rb, rw = _byte_mem(words={0x4FD0 + i * 0x12 + 4: 0xFFFF for i in range(12)}, byts={0x7B19: 5})
    writes, sfx, hit, slot = projectile_vs_enemies(rb, rw, 0x4F0A)
    assert hit is False and slot is None and sfx == []


def test_projectile_knockback_hit_consumes_source():
    SI, DI = 0x4F0A, 0x4FD0
    rb, rw = _byte_mem(
        words={SI: 0x100, SI + 2: 0x100, SI + 4: 0x0000,    # source pos + id 0
               DI: 0x100, DI + 2: 0x100, DI + 4: 0x0000,    # enemy pos + id 0 (not 0xFFFF -> occupied)
               DI + 6: 0x600, DI + 8: 0x10},                # def ptr + Xvel for knockback
        byts={DI + 0xE: 0x00, DI + 0xF: 0x10,               # alive, HP 0x10 (> damage -> survives)
              0x604: 0x00, 0x7B19: 0x05,                    # [def+4] collidable, damage 5
              0x4FD5: 0x00,                                  # [di+5] starts 0
              0xA312: 1, HALF_LO: 0x10, HALF_LO + 1: 0x10, HALF_WX: 0x08})
    writes, sfx, hit, slot = projectile_vs_enemies(rb, rw, SI)
    word = lambda o: writes.get(o, 0) | (writes.get(o + 1, 0) << 8)
    assert hit is True and slot == DI and sfx == []
    assert writes[DI + 5] == 0x40                # hit flag OR'd in
    assert writes[DI + 0xF] == 0x0B              # HP 0x10 - 5
    assert word(DI) == 0xFC                      # knockback: 0x100 - (0x10 >> 2)
    assert word(SI + 4) == 0xFFFF                # source (projectile) consumed
