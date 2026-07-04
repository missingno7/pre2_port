"""Bridge: read the firefly swarm slots (1A0F:0x6EA9) and camera/page for the faithful renderer.

Pure segment:offset layout only — no gameplay decisions. The swarm pass (54AB) runs with ds=1A0F, so
every operand here is in the data segment: the 20-slot array at 0x6EA9 (stride 8), the camera
[0x2DE4]/[0x2DE6], and the back page [0x2DD8].
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List

from pre2.bridge.dgroup_view import SwarmView
from pre2.recovered.fireflies import Firefly


@dataclass
class FireflyState:
    slots: List[Firefly]
    cam_col: int
    cam_row: int
    page: int
    slot_idx: List[int] = None    # physical slot index (0..19) parallel to `slots`; persistent per firefly,
                                  # so the enhanced renderer matches prev/cur by it to interpolate the drift.
                                  # The faithful draw ignores it.


def read_fireflies(mem) -> FireflyState:
    """Read the live firefly slots + camera/page for the faithful renderer, via the human-named ``SwarmView``
    (the byte-backed layout bridge — the one place the 0x6EA9 array's stride/fields live). The physical slot
    index (0..19) is kept parallel for the enhanced renderer's per-firefly drift interpolation; the faithful
    draw ignores it."""
    view = SwarmView(mem)
    slots: List[Firefly] = []
    slot_idx: List[int] = []
    for i, slot in enumerate(view.slots):
        if not slot.alive:
            continue
        slots.append((slot.x, slot.y, slot.timer))
        slot_idx.append(i)
    return FireflyState(slots=slots, cam_col=view.cam_col, cam_row=view.cam_row, page=view.page, slot_idx=slot_idx)
