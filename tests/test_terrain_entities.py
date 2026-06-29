"""Byte-exact regression for the 1030:4907 terrain-entity system (see
:mod:`pre2.recovered.terrain_entities`).

Goldens captured from the original ASM under the VM (faithful hybrid replay of the 233821 + gorilla demos with
4907 un-hooked): per active call, every read ``tick_terrain_entities`` makes (DGROUP word ``rw`` / byte ``rb`` /
level-map ``tile``) and the ASM's after-value at each contract offset. The recovered whole-routine transform
must reproduce that contract exactly. At capture, recovered-vs-ASM was 0 mismatches over the captured calls,
and a whole-DGROUP shadow was 0 divergences over 1116 calls across three demos (excluding only the audio-ISR
scratch). The contract spans entity movement, the 0x5570 render projection, and the 4B05 player-ride collision.
"""
from __future__ import annotations

import json
from pathlib import Path

from pre2.recovered.terrain_entities import tick_terrain_entities

_FIX = Path(__file__).parent / "fixtures" / "terrain_entities" / "tick_terrain_entities.json"


def test_tick_terrain_entities_byte_exact():
    cases = json.loads(_FIX.read_text())
    assert cases, "no fixture cases"
    for case in cases:
        rw_d = {int(k): v for k, v in case["rw"].items()}
        rb_d = {int(k): v for k, v in case["rb"].items()}
        tile_d = {int(k): v for k, v in case["tile"].items()}
        golden = {int(k): v for k, v in case["writes"].items()}
        writes = tick_terrain_entities(lambda o: rw_d[o & 0xFFFF], lambda o: rb_d[o & 0xFFFF],
                                       lambda o: tile_d[o & 0xFFFF])
        assert writes == golden
