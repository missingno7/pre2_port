"""Tests for the player<->world interaction island keystones (pre2.recovered.player_interaction).

Byte-exact ASM equivalence is checked live by pre2/probes/probe_player_interaction.py (thin witness — the
demos rarely pickup/stomp); these pin the score/spawn/consume + anim-advance contracts with fixtures."""
from __future__ import annotations

from pre2.recovered.player_interaction import (spawn_pickup_effect, advance_anim_script,
                                               _knockback, loop1, loop2_handler, Loop2NeedsHelper,
                                               CLUB_TYPE, LEVEL, LEVEL_DONE, LIGHT_STATE, LETTERS_MASK)


def _mem(kv):
    rb = lambda o: kv.get(o & 0xFFFF, 0) & 0xFF
    rw = lambda o: rb(o) | (rb((o + 1) & 0xFFFF) << 8)
    return rb, rw


def test_spawn_collectible_adds_score_and_spawns_effect():
    kv = {0xA353: 0x64,                         # score table[id 0x4a] = 0x64
          0x6C0E: 0x00, 0x6C0F: 0x10,           # score low = 0x1000
          0x5454: 0xFF, 0x5455: 0xFF,           # effect slot 0 ([+4]) free
          0x4FD0: 0x34, 0x4FD1: 0x12,           # src X = 0x1234
          0x4FD2: 0x78, 0x4FD3: 0x56}           # src Y = 0x5678
    rb, rw = _mem(kv)
    w = spawn_pickup_effect(rb, rw, 0x4A, 0x4FD0)
    assert w[0x6C0E] == (0x1064, 2) and w[0x6C10] == (0, 2)   # 32-bit score += 0x64
    assert w[0x5454] == (0x4A, 2)                              # effect id
    assert w[0x5450] == (0x1234, 2) and w[0x5452] == (0x5678, 2)  # X, Y from src
    assert w[0x545C] == (0x2C, 2) and w[0xA33E] == (0x5450, 2)    # [+0xc], effect ptr


def test_spawn_non_collectible_id_no_score():
    rb, rw = _mem({0x5454: 0xFF, 0x5455: 0xFF})
    w = spawn_pickup_effect(rb, rw, 0x100, 0x4FD0)            # 0x100 not in 0x4a..0x5a
    assert 0x6C0E not in w and w[0x5454] == (0x100, 2)        # no score, still spawns


def test_spawn_no_free_slot_does_nothing():
    rb, rw = _mem({})                                         # all slots non-free (0 != 0xFFFF)
    assert spawn_pickup_effect(rb, rw, 0x100, 0x4FD0) == {}


def test_spawn_loop2_entity_consumes_linked():
    kv = {0x5454: 0xFF, 0x5455: 0xFF, 0x50A8 + 9: 0x00, 0x50A8 + 0xA: 0x60}   # [si+9] = 0x6000
    rb, rw = _mem(kv)
    w = spawn_pickup_effect(rb, rw, 0x100, 0x50A8)            # si >= 0x50A8 (loop2)
    assert w[0x6000 + 4] == (0xFFFF, 2)                       # linked entity consumed


def test_advance_anim_script_skips_to_after_marker():
    kv = {0x4FD0 + 0xC: 0x00, 0x4FD0 + 0xD: 0x70,            # [di+0xc] = 0x7000
          0x7004: 0x00, 0x7005: 0x7D}                         # [0x7004] = 0x7D00 marker
    rb, rw = _mem(kv)
    w = advance_anim_script(rw, 0x4FD0)                       # 0x7000 -> +2 ->7002 ->7004(marker) ->+2 ->7006
    assert w[0x4FD0 + 0xC] == (0x7006, 2)


# --- loop1 (player-vs-enemy collision) — byte-exact shadow in probe_player_interaction (107 ticks); these
#     pin the control flow without the VM.
def test_knockback_player_up():
    rb, rw = _mem({0x4F1E: 0x00, 0x4F1F: 0x01, 0xA331: 0x10})   # player Y=0x100 (word), knockback delta=0x10
    w = _knockback(rb, rw, 0xFFC0)
    assert w[0x4F2A] == (0xFFC0, 2) and w[0x6BD2] == (0, 1) and w[0x4F1E] == (0x00F0, 2)


def test_loop1_skips_when_player_dying():
    rb, rw = _mem({0x4F2D: 0x2C})                            # player death-state != 0 -> straight to loop2
    applied = []
    assert loop1(rb, rw, lambda w: applied.append(w), lambda s: None) is False
    assert applied == []


def test_loop1_no_objects_no_writes():
    kv = {}
    for k in range(12):                                      # all 12 slots empty ([+4]==0xFFFF)
        kv[0x4FD0 + k * 0x12 + 4] = 0xFF
        kv[0x4FD0 + k * 0x12 + 5] = 0xFF
    rb, rw = _mem(kv)
    applied = []
    assert loop1(rb, rw, lambda w: applied.append(w), lambda s: None) is False
    assert applied == []


# --- loop2 effect handlers (byte-exact shadow in probe_player_interaction, 143 ticks); names per cyxx ---
def test_loop2_club_type():                                  # num 0xD (id 0x42) -> club/weapon type 0
    rb, rw = _mem({0x50A8 + 9: 0xFF, 0x50A8 + 0xA: 0xFF})    # no linked item
    w, sfx = loop2_handler(0xD, rb, rw, 0x50A8, lambda: None)
    assert w[CLUB_TYPE] == (0, 1) and sfx == [8]


def test_loop2_end_of_level_transition():                   # num 0xE2 (id 0x117): level 2 -> 12 + complete
    rb, rw = _mem({0x2D8A: 2})
    w, sfx = loop2_handler(0xE2, rb, rw, 0x50A8, lambda: None)
    assert w[LEVEL] == (0xC, 1) and w[LEVEL_DONE] == (1, 1)


def test_loop2_bonus_letter():                              # num 0x27 (id 0x4c) -> set letter bit 0
    rb, rw = _mem({0x50A8 + 9: 0xFF, 0x50A8 + 0xA: 0xFF})
    w, sfx = loop2_handler(0x27, rb, rw, 0x50A8, lambda: None)
    assert w[LETTERS_MASK] == (1, 1) and sfx == [8]


def test_loop2_light_off():                                 # num 0xB5 (id 0xea): light on -> off
    rb, rw = _mem({0x6C04: 0, 0x50A8 + 9: 0xFF, 0x50A8 + 0xA: 0xFF})
    w, sfx = loop2_handler(0xB5, rb, rw, 0x50A8, lambda: None)
    assert w[LIGHT_STATE] == (1, 1) and w[0x6C01] == (1, 1) and sfx == [1]


def test_loop2_unmapped_num_raises():                       # the defensive fallback for an id no handler claims
    import pytest
    with pytest.raises(Loop2NeedsHelper):
        loop2_handler(0xFF, *_mem({}), 0x50A8, lambda: None)


def _burst_arena(kv):
    """Free the entity list (0x50A8..0x52E8, the 8D1B burst target) + the effect list (0x5450)."""
    for s in range(52):
        kv[0x50A8 + s * 0x12 + 4] = 0xFF; kv[0x50A8 + s * 0x12 + 5] = 0xFF
    for s in range(0x10):
        kv[0x5450 + s * 0x12 + 4] = 0xFF; kv[0x5450 + s * 0x12 + 5] = 0xFF
    return kv


def test_loop2_trap_hurts_and_scatters_bones():            # num 0xA7 (id 0xdc) [864F] ASM_MATCHED
    kv = _burst_arena({0x4F1C: 0x00, 0x4F1D: 0x02, 0x4F1E: 0x40, 0x4F1F: 0x01,  # player (0x200,0x140)
                       0x27D6: 2, 0x6BC9: 0,                # ENERGY=2 -> 12 bones; bonus 0
                       0x50A8 + 9: 0xFF, 0x50A8 + 0xA: 0xFF})
    rb, rw = _mem(kv)
    w, sfx = loop2_handler(0xA7, rb, rw, 0x50A8, lambda: None)
    assert sfx == [1]
    assert w[0x4F2D] == (0x2C, 1) and w[0x6BEA] == (7, 1)   # hurt state + shake
    assert w[0x27D6] == (0, 1)                              # energy zeroed by the bone burst
    assert w[0x50A8 + 4] == (0x46, 1) and w[0x50A8 + 5] == (0x20, 1)  # first bone sprite id 0x2046
    assert w[0x5454] == (0xE4, 1) and w[0x5455] == (0x00, 1)  # 0xe4 pickup effect


def test_boss_projectile_death_when_no_energy():           # [8618] ENERGY==0 -> 65B3 _offcamera_trigger
    from pre2.recovered.player_interaction import _boss_projectile
    rb, rw = _mem({0x27D6: 0, 0x27D8: 3, 0x6BE4: 0})        # energy 0, lives 3, not yet triggered
    w, sfx = _boss_projectile(rb, rw)
    assert sfx == []
    assert w[0x27D8] == (2, 1) and w[0x6BE4] == (2, 1)      # lose a life + arm respawn


def test_boss_projectile_survives_with_energy():           # [8618] ENERGY>=1 -> hurt + -1 energy + bones
    from pre2.recovered.player_interaction import _boss_projectile
    rb, rw = _mem(_burst_arena({0x27D6: 2, 0x4F1C: 0, 0x4F1D: 2, 0x4F1E: 0x40, 0x4F1F: 1, 0x6BC9: 0}))
    w, sfx = _boss_projectile(rb, rw)
    assert sfx == [1]
    assert w[0x4F2D] == (0x2C, 1)                           # hurt state
    assert w[0x27D6] == (1, 1)                              # energy 2 -> 1 (restored after the 6-bone burst)


def _bomb_fixture():
    """One active on-screen enemy (object slot 0) at (0x100,0x80); all entity/effect slots free; rng seeded."""
    kv = {0x4FD0 + 4: 0x46, 0x4FD0 + 5: 0x20,               # slot0 active + on-screen
          0x4FD0 + 6: 0x00, 0x4FD0 + 7: 0x70,               # def ptr = 0x7000 ([0x7004]=0 -> no 0x10 flag)
          0x4FD0 + 0: 0x00, 0x4FD0 + 1: 0x01,               # X = 0x100
          0x4FD0 + 2: 0x80, 0x4FD0 + 3: 0x00,               # Y = 0x80
          0x2CEC: 0x12, 0x2CED: 0x34, 0x2CEE: 0x56, 0x2CEF: 0x78, 0x2CF0: 0x9A}  # rng state
    for s in range(1, 12):                                   # other object slots inactive
        kv[0x4FD0 + s * 0x12 + 4] = 0xFF; kv[0x4FD0 + s * 0x12 + 5] = 0xFF
    for s in range(52):                                      # entity list free
        kv[0x50A8 + s * 0x12 + 4] = 0xFF; kv[0x50A8 + s * 0x12 + 5] = 0xFF
    for s in range(0x10):                                    # effect list free
        kv[0x5450 + s * 0x12 + 4] = 0xFF; kv[0x5450 + s * 0x12 + 5] = 0xFF
    return kv


def test_loop2_bomb_kills_enemy_and_spawns_food():          # num 0xAA (id 0xdf) [870A] bomb (byte-exact in probe)
    from pre2.recovered.combat_interaction import roll_bonus_sprite_id
    rb, rw = _mem(_bomb_fixture())
    w, sfx = loop2_handler(0xAA, rb, rw, 0x50A8, lambda: None)
    assert sfx == [0]
    assert w[0x4FD0 + 0xE] == (0xFF, 1)                      # enemy marked dead
    assert w[0x4FD0 + 4] == (0xFF, 1) and w[0x4FD0 + 5] == (0xFF, 1)  # enemy slot freed
    assert w[0xA336] == (0x00, 1) and w[0xA337] == (0x01, 1)  # SPAWN_X = enemy X 0x100
    assert w[0xA338] == (0x80, 1) and w[0xA339] == (0x00, 1)  # SPAWN_Y = enemy Y 0x80
    assert w[0x2CEC] != (0x12, 1)                            # rng advanced by the food rolls
    sid0, _ = roll_bonus_sprite_id((0x12, 0x34, 0x56, 0x789A))
    assert 0x2080 <= sid0 <= 0x20DE                          # first food id in range
    assert w[0x5450 + 4] == (0xE7, 1) and w[0x5450 + 5] == (0x00, 1)  # the 0xE7 pickup effect


def test_death_offcamera_path_returns_tuples():             # the no-energy game-over death (uncovered by demos)
    # [83C8] energy goes negative -> _death calls 65B3 _offcamera_trigger, whose byte-level {off:int} must be
    # wrapped as (val,width) tuples or the live apply crashes ("cannot unpack non-iterable int").
    from pre2.recovered.player_interaction import _death
    rb, rw = _mem({0x6BC5: 0, 0x27D6: 0, 0x27D8: 3, 0x6BE4: 0,   # energy 0 -> dec negative -> respawn trigger
                   0x4FD0 + 6: 0x00, 0x4FD0 + 7: 0x70})           # [di+6] def ptr = 0x7000
    out, sfx = _death(rb, rw, 0x4FD0)
    assert all(isinstance(v, tuple) and len(v) == 2 for v in out.values()), out
    assert out[0x27D8] == (2, 1)                             # _offcamera_trigger lives 3->2, wrapped


def test_loop2_extra_life():                                # num 0xAE (id 0xe3) [87E6] +1 life (byte-exact in probe)
    kv = {0x27D8: 3,                                         # current lives = 3
          0x4F1C: 0x00, 0x4F1D: 0x02, 0x4F1E: 0x40, 0x4F1F: 0x01,  # player at (0x200, 0x140)
          0x50A8 + 9: 0xFF, 0x50A8 + 0xA: 0xFF}              # entity, no link
    for s in range(0x10):
        kv[0x5450 + s * 0x12 + 4] = 0xFF; kv[0x5450 + s * 0x12 + 5] = 0xFF
    rb, rw = _mem(kv)
    w, sfx = loop2_handler(0xAE, rb, rw, 0x50A8, lambda: None)
    assert sfx == [4]
    assert w[0x27D8] == (4, 1)                               # lives 3 -> 4 (byte)
    assert w[0x5454] == (0xE3, 2)                            # 0xe3 effect spawned (spawn_pickup_effect word)
    assert w[0x5450] == (0x200, 2)                           # effect X = player X 0x200 (word)
    assert w[0x5452] == (0x140, 2)                           # effect Y = player Y 0x140 (word)


def test_loop2_extra_life_caps_at_99():                     # lives == 99 -> no increment
    rb, rw = _mem({0x27D8: 0x63, 0x50A8 + 9: 0xFF, 0x50A8 + 0xA: 0xFF,
                   **{0x5450 + s * 0x12 + 4: 0xFF for s in range(0x10)},
                   **{0x5450 + s * 0x12 + 5: 0xFF for s in range(0x10)}})
    w, sfx = loop2_handler(0xAE, rb, rw, 0x50A8, lambda: None)
    assert 0x27D8 not in w                                   # capped, no life write


def test_loop2_grenade_no_enemies_shake_and_effect():       # num 0xA9 (id 0xde) [86B7] grenade, walk skips
    kv = {}
    for s in range(12):                                      # no active enemies
        kv[0x4FD0 + s * 0x12 + 4] = 0xFF; kv[0x4FD0 + s * 0x12 + 5] = 0xFF
    kv[0x50A8 + 9] = 0xFF; kv[0x50A8 + 0xA] = 0xFF           # bomb entity, no link
    for s in range(0x10):
        kv[0x5450 + s * 0x12 + 4] = 0xFF; kv[0x5450 + s * 0x12 + 5] = 0xFF
    rb, rw = _mem(kv)
    w, sfx = loop2_handler(0xA9, rb, rw, 0x50A8, lambda: None)
    assert sfx == [0]
    assert w[0x6BEA] == (9, 1)                               # screen shake set
    assert w[0x5450 + 4] == (0xE6, 1) and w[0x5450 + 5] == (0x00, 1)  # the 0xE6 effect
