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


def test_loop2_deferred_path_raises():                      # num 0xAA (id 0xdf) bomb -> not recovered yet
    import pytest
    with pytest.raises(Loop2NeedsHelper):
        loop2_handler(0xAA, *_mem({}), 0x50A8, lambda: None)
