"""Recovered PRE2 level-password generator (1030:932F). The byte-exact ASM equivalence is proven by invoking
the original routine in the VM (pre2/probes/verify_password.py: idx 0..0x12 all match); these pin the known
codes + the formula's structure."""
from __future__ import annotations

from pre2.recovered.password import (
    DEFAULT_SEED,
    bios_seed,
    level_code,
    password,
    password_table,
    validate_code,
)


def test_known_level1_codes():
    assert password(1, expert=False) == "A305"   # verified in-game
    assert password(1, expert=True) == "A905"     # verified in-game


def test_bios_seed_zeroed_falls_back_to_0x20():
    # [asm 932F 938c-9390] a zeroed BIOS/option-ROM region (the GOG build under the VM/DOSBox) sums to 0, so the
    # seed is the 0x20 fallback (== DEFAULT_SEED) -- the value the VM actually reaches (its [0xA333]=0x20 at
    # gameplay), which is why this build's L1 codes are the deterministic A305/A905.
    assert bios_seed(bytearray(0x100000)) == 0x20 == DEFAULT_SEED


def test_bios_seed_sums_the_f000_date_bytes():
    # [asm 934d-9350] the F000:FFF0..FFFF date bytes fold into dl (add) / dh (sbb); a single 0x07 date byte gives
    # dl=0x07, dh = -(0x07) = 0xF9 -> dx=0xF907 (nonzero, so no fallback). Pins the exact byte arithmetic.
    mem = bytearray(0x100000)
    mem[0xFFFF0] = 0x07
    assert bios_seed(mem) == 0xF907


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


# --- the native front-end ENTER-CODE accumulation (front_end._password_step, 1030:9985/99AA-9ADF) ---------------
from pre2.native.front_end import _password_init, _password_step   # noqa: E402
from pre2.native.state import DATA_SEG, NativeGameState            # noqa: E402

_DS = DATA_SEG << 4
_SCAN = {"A": 0x1E, "B": 0x30, "C": 0x2E, "D": 0x20, "E": 0x12, "F": 0x21, "0": 0x0B, "1": 0x02, "2": 0x03,
         "3": 0x04, "4": 0x05, "5": 0x06, "6": 0x07, "7": 0x08, "8": 0x09, "9": 0x0A}   # make code per hex char


def _pw_state():
    st = NativeGameState(bytearray(0x100000))
    st.data[(0x1030 << 4) + 5] = 3                          # cs:[5] rotate count
    for ch, sc in _SCAN.items():
        st.data[_DS + 0xB068 + sc] = ord(ch)               # the [0xB068] scancode->ASCII table
    st.data[_DS + 0xB068 + 0x39] = 0x2D                    # space -> '-' (ignored)
    _password_init(st)
    return st


def _pw_enter(st, code):
    r = None
    for ch in code:
        st.data[_DS + 0x2874] = _SCAN[ch]                  # the runner's scancode latch [asm 99BE]
        r = _password_step(st)
    return r


def test_password_step_accepts_valid_code_and_selects_level():
    st = _pw_state()
    assert _pw_enter(st, "A305") == (0, False) and st.data[_DS + 0x2D8A] == 0 and st.data[_DS + 0xB197] == 0
    st = _pw_state()
    assert _pw_enter(st, "A105") == (2, False) and st.data[_DS + 0x2D8A] == 2      # L3 beginner
    st = _pw_state()
    assert _pw_enter(st, "A905") == (0, True) and st.data[_DS + 0x2D8A] == 0 and st.data[_DS + 0xB197] == 1


def test_password_step_rejects_and_resets():
    st = _pw_state()
    st.data[_DS + 0x2D8A] = 7
    assert _pw_enter(st, "1111") is None
    assert st.data[_DS + 0x2D8A] == 7                       # a bad code selects nothing
    assert bytes(st.data[_DS + 0xB170:_DS + 0xB174]) == b"[[[["   # buffer reset for a retry


def test_password_step_validates_only_on_fourth_char_and_ignores_non_hex():
    st = _pw_state()
    for n, ch in enumerate("A10"):
        st.data[_DS + 0x2874] = _SCAN[ch]
        assert _password_step(st) is None and st.data[_DS + 0xB1A8] == n + 1
    st.data[_DS + 0x2874] = 0x39                            # space -> '-' -> ignored (no 4th char)
    assert _password_step(st) is None and st.data[_DS + 0xB1A8] == 3
    st.data[_DS + 0x2874] = _SCAN["5"]
    assert _password_step(st) == (2, False)
