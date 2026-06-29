"""Byte-exact regression for the 1030:4907 terrain-entity MOVEMENT half (see
:mod:`pre2.recovered.terrain_entities`).

Goldens captured from the original ASM under the VM (faithful hybrid replay of the gorilla + 233821 demos with
4907 un-hooked): per active call, every read ``move_entities`` makes (DGROUP word ``rw`` / byte ``rb`` /
level-map ``tile``) and the 0x9107 source-list before/after. The recovered movement must reproduce the ASM
source window EXCLUDING each slot's ``[+6]`` byte (which the player-ride collision tail toggles, not the
movement). At capture, recovered-vs-ASM was 0 mismatches over 928 live calls. The render projection (0x5570) +
player-ride collision (4B05) are separate stages, not covered here.
"""
from __future__ import annotations

import json
from pathlib import Path

from pre2.recovered.terrain_entities import ENTITY_N, SRC_STRIDE, move_entities

_FIX = Path(__file__).parent / "fixtures" / "terrain_entities" / "move_entities.json"
_LO = 0x9107
_EXCL = {k * SRC_STRIDE + 6 for k in range(ENTITY_N)}      # [+6] per slot — toggled by the collision tail


def test_move_entities_byte_exact():
    cases = json.loads(_FIX.read_text())
    assert cases, "no fixture cases"
    for case in cases:
        rw_d = {int(k): v for k, v in case["rw"].items()}
        rb_d = {int(k): v for k, v in case["rb"].items()}
        tile_d = {int(k): v for k, v in case["tile"].items()}
        before = bytes.fromhex(case["before"])
        after = bytes.fromhex(case["after"])
        writes = move_entities(lambda o: rw_d[o & 0xFFFF], lambda o: rb_d[o & 0xFFFF],
                               lambda o: tile_d[o & 0xFFFF])
        pred = bytearray(before)
        for off, (val, wid) in writes.items():
            loc = (off - _LO) & 0xFFFF
            assert loc < len(pred), f"move_entities wrote {off:#06x} outside the source window"
            pred[loc] = val & 0xFF
            if wid == 2 and loc + 1 < len(pred):
                pred[loc + 1] = (val >> 8) & 0xFF
        for i in range(len(after)):
            if i in _EXCL:
                continue
            assert pred[i] == after[i], f"move_entities slot {i // SRC_STRIDE} off+{i % SRC_STRIDE:#x} mismatch"
