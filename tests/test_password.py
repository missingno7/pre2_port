"""Recovered PRE2 level-password generator (1030:932F). The byte-exact ASM equivalence is proven by invoking
the original routine in the VM (pre2/probes/verify_password.py: idx 0..0x12 all match); these pin the known
codes + the formula's structure."""
from __future__ import annotations

from pre2.recovered.password import (
    DEFAULT_SEED,
    level_code,
    password,
    password_table,
    validate_code,
)


def test_known_level1_codes():
    assert password(1, expert=False) == "A305"   # verified in-game
    assert password(1, expert=True) == "A905"     # verified in-game


def test_index_mapping_beginner_then_expert():
    # index 0 == L1 beginner, index 10 == L1 expert
    assert level_code(0) == 0xA305
    assert level_code(10) == 0xA905
    assert password(2, expert=False) == "A205"    # index 1
    assert password(1, expert=True) == f"{level_code(10):04X}"


def test_formula_rol_of_xor_times_seed():
    # code = rol16((index ^ 0x55A3) * seed, 3)
    idx, seed = 0, DEFAULT_SEED
    v = ((idx ^ 0x55A3) * seed) & 0xFFFF
    expect = ((v << 3) | (v >> 13)) & 0xFFFF
    assert level_code(idx, seed, 3) == expect == 0xA305


def test_seed_parameter_changes_codes():
    # the password is BIOS/seed specific -> a different seed yields different codes
    assert level_code(0, seed=0x21) != level_code(0, seed=0x20)


def test_table_has_ten_levels_each_mode():
    t = password_table()
    assert len(t) == 10
    assert t[0] == (1, "A305", "A905")
    assert all(len(beg) == 4 and len(exp) == 4 for _, beg, exp in t)


def test_validate_code_round_trips_every_index():
    # the ENTER-CODE validator (9A6E) accepts every level password and decodes its level + difficulty
    for index in range(0x13):                       # the 9A70 loop accepts indices 0..0x12
        expert = index >= 10
        level = index - 10 if expert else index
        assert validate_code(level_code(index)) == (level, expert)


def test_validate_code_known_codes():
    assert validate_code(0xA305) == (0, False)      # L1 beginner ([0x2D8A]=0)
    assert validate_code(0xA905) == (0, True)       # L1 expert


def test_validate_code_rejects_unknown():
    # a value matching no level password is rejected (the menu stays on ENTER CODE)
    valid = {level_code(i) for i in range(0x13)}
    bogus = next(v for v in range(0x10000) if v not in valid)
    assert validate_code(bogus) is None
