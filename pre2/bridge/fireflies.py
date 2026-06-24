"""Bridge: read the firefly swarm slots (1A0F:0x6EA9) and camera/page for the faithful renderer.

Pure segment:offset layout only — no gameplay decisions. The swarm pass (54AB) runs with ds=1A0F, so
every operand here is in the data segment: the 20-slot array at 0x6EA9 (stride 8), the camera
[0x2DE4]/[0x2DE6], and the back page [0x2DD8].
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from pre2.recovered.fireflies import Firefly

_DATA_SEG = 0x1A0F
_ARRAY = 0x6EA9
_END = 0x6F49          # cmp si,0x6f49 ; jae  -> 20 slots of 8 bytes
_STRIDE = 8
_DEAD = 0x55AA         # cmp ax,0x55aa ; je  -> dead slot


@dataclass
class FireflyState:
    slots: List[Firefly]
    cam_col: int
    cam_row: int
    page: int


def _r16(mem, off: int) -> int:
    base = (_DATA_SEG << 4) + off
    return mem.data[base] | (mem.data[base + 1] << 8)


def _s16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


def read_fireflies(mem) -> FireflyState:
    slots: List[Firefly] = []
    for off in range(_ARRAY, _END, _STRIDE):
        x = _r16(mem, off)
        if x == _DEAD:
            continue
        y = _r16(mem, off + 2)
        timer = mem.data[(_DATA_SEG << 4) + off + 6]
        slots.append((_s16(x), _s16(y), timer))
    return FireflyState(
        slots=slots,
        cam_col=_s16(_r16(mem, 0x2DE4)),
        cam_row=_s16(_r16(mem, 0x2DE6)),
        page=_r16(mem, 0x2DD8),
    )
