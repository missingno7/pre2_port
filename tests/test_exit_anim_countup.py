"""Regression test for the level-end TALLY food-into-pot score COUNT-UP (native_exit_anim, [asm 4CCB/4DF5-4F0B]).

The count-up spawns each collected food item ([0x6C12] queue) at the top of the tally screen and drops it into
the pot, adding its value to the score. Two bugs made it wrong before:

  1. The VM clears the object/effect slots at the iris-close (316F) before the tally; native skipped it, so the
     count-up scanned the LEVEL's leftover [0x52E8] effect sprites (already past the Y>=0x91 collect line) and
     OVER-counted (score 610 vs the VM's 200 on this witness).
  2. The loop terminated every frame, so it quit the instant the last item spawned — before it fell — leaving the
     score uncounted (0 added).

Witness: the DGROUP of snapshot_pre2_20260702_111016 (a level-1 exit with one bonus collected; score 100, the
[0x6C12] queue holds one item worth 100). The VM oracle counts it up to 200. Rendering is stubbed (the fixture is
the DGROUP only, not the sprite bank) — this exercises the count-up STATE math, clear, refill and termination.
"""
from __future__ import annotations

import zlib
from pathlib import Path

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "exit_anim" / "dgroup_pre_exit.zz"
DS = 0x1A0F << 4


def _load_state():
    from pre2.native.state import NativeGameState
    data = bytearray(0x100000)
    data[DS:DS + 0x10000] = zlib.decompress(FIXTURE.read_bytes())
    return NativeGameState(data)


def _score(state):
    d = state.data
    return d[DS + 0x6C0E] | (d[DS + 0x6C0F] << 8) | (d[DS + 0x6C10] << 16) | (d[DS + 0x6C11] << 24)


def test_exit_anim_countup_matches_vm(monkeypatch):
    """native_exit_anim counts the one collected item up 100 -> 200 (the VM oracle) — not 610 (over-count) or
    100 (no count). Verifies the 316F object-clear + the refill/termination loop shape together."""
    from dos_re.dos import DOSMachine
    import pre2.views.tally_scene as tally_scene
    from pre2.native.runtime import native_exit_anim

    # stub the tally render (the fixture is the DGROUP only; the count-up state math needs no pixels)
    monkeypatch.setattr(tally_scene, "build_tally_scene",
                        lambda *a, **k: ([bytearray(0x10000) for _ in range(4)], k.get("page", 0)))

    state = _load_state()
    assert _score(state) == 100                                   # the witness starts at 100
    assert state.data[DS + 0x6C9E] != 0                           # a bonus was collected -> the count-up runs
    # 12 leftover level effect sprites sit in [0x52E8] at the snapshot (the over-count source)
    leftover = sum(1 for k in range(0x14)
                   if (state.data[DS + 0x52E8 + k * 0x12 + 4] | (state.data[DS + 0x52E8 + k * 0x12 + 5] << 8)) != 0xFFFF)
    assert leftover == 12

    disp = state.data[DS + 0x2DD6] | (state.data[DS + 0x2DD7] << 8)
    frames = 0
    for _ in native_exit_anim(state, DOSMachine(str(Path(__file__).resolve().parents[1] / "assets")),
                              disp, game_root=str(Path(__file__).resolve().parents[1] / "assets")):
        frames += 1
        assert frames < 6000, "count-up did not terminate"

    assert _score(state) == 200                                   # the VM oracle: +100 (only the queued item)
    # every [0x52E8] slot ends free (the leftovers were cleared, the queued item collected + freed)
    assert all((state.data[DS + 0x52E8 + k * 0x12 + 4] | (state.data[DS + 0x52E8 + k * 0x12 + 5] << 8)) == 0xFFFF
               for k in range(0x14))
