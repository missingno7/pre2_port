"""The 'YOU MUST BE AN EXPERT EATER' wall (pre2.native.front_end) — main's 0x0163 gate.

A BEGINNER ([0xB197]==0) who advances into level 8 or 9 ([0x2D8A] in {8,9}) is shown CASTLE.SQZ (resource
0x2C) and sent back to the menu, instead of loading the level — beginner can only PLAY levels 0..7 (penguin at
8 is the expert-only wall). Traced from the demo (demo_pre2_20260712_122007) where the VM hits 0x0178 at
[0x2D8A]=8/beginner.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from pre2.native.front_end import is_expert_eater_wall
from pre2.native.state import NativeGameState

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
_DS = 0x1A0F << 4


def _state(level, mode):
    st = NativeGameState(bytearray(0x110000))
    st.data[_DS + 0x2D8A] = level
    st.data[_DS + 0xB197] = mode
    return st


def test_wall_gate_beginner_only_levels_8_and_9():
    # [asm 0163-0176] level in {8,9} AND beginner -> the wall
    assert is_expert_eater_wall(_state(8, 0))          # beginner penguin (8) -> wall
    assert is_expert_eater_wall(_state(9, 0))          # beginner 9 -> wall
    # everything else plays normally
    assert not is_expert_eater_wall(_state(7, 0))      # beginner 0..7 are playable
    assert not is_expert_eater_wall(_state(0, 0))
    assert not is_expert_eater_wall(_state(0x0A, 0))   # bonus levels (>=0xA) are not the wall
    assert not is_expert_eater_wall(_state(8, 1))      # EXPERT plays 8/9
    assert not is_expert_eater_wall(_state(9, 1))


def test_carte_and_load_raises_before_yielding_any_carte_frame():
    # [asm 0163] the gate is checked BEFORE the carte (9520) and the loader (447d). A beginner who chose level 8
    # must NOT see the world-map scroll-in or the level load: native_carte_and_load raises on the FIRST next(),
    # before it yields a single carte FrontEndScene. (This is the bug the user hit — the wall showed AFTER the
    # carte + level reveal because the old gate was at the top of the gameplay loop, past the load.)
    from pre2.gaps import Pre2ExpertEater
    from pre2.native.front_end import native_carte_and_load
    gen = native_carte_and_load(_state(8, 0), None, str(ASSETS))
    with pytest.raises(Pre2ExpertEater):
        next(gen)                                          # raises before any carte frame is produced


def test_native_level_end_raises_after_advance_before_load():
    # progression: finishing beginner level 7 advances [0x2D8A] 7->8, then the gate fires BEFORE native_level_start
    # loads level 8. Assert it raised (so no load ran) AND the advance happened (so the wall condition is real).
    from pre2.gaps import Pre2ExpertEater
    from pre2.native.level_state import native_level_end
    st = _state(7, 0)                                      # beginner, on level 7 (the last playable beginner level)
    st.data[_DS + 0x6BE6] = 1                              # [asm 4C69] a normal end (+1), not a warp
    with pytest.raises(Pre2ExpertEater):
        native_level_end(st, game_root=str(ASSETS))
    assert st.data[_DS + 0x2D8A] == 8                      # advanced into the penguin -> the wall (level never loaded)


@pytest.mark.skipif(not (ASSETS / "CASTLE.SQZ").exists(), reason="game assets not present")
def test_native_expert_eater_renders_castle_scene():
    from pre2.native.front_end import MODE_LINEAR, native_expert_eater
    st = _state(8, 0)
    gen = native_expert_eater(st, str(ASSETS))
    frame = next(gen)                                  # the first fade-in frame
    assert frame.mode == MODE_LINEAR
    assert frame.linear is not None and len(frame.linear) == 64000   # the CASTLE.SQZ 13h image
    assert len(frame.palette) == 256
    # the fade-in produces multiple frames before the hold/wait
    assert sum(1 for _ in zip(range(5), gen)) >= 1
