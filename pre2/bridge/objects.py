"""Memory views for the object draw path (VM memory ⇄ recovered dataclasses).

The one place that knows *where* the object draw state lives in PRE2 memory. Draw
logic lives in ``pre2/recovered/object_draw.py``; this module only translates layout.

Factual naming only (no Player/Enemy yet — promote to archetypes only when field
usage + ASM evidence support it). See docs/pre2/symbol_ledger.md ("Object-list draw").

Currently models the inputs the per-object draw primitive (``1030:6544``) consumes;
the multi-tile structure table at ``0x83EF`` (15 slots × 10 bytes) is characterized in
the ledger and will be modelled here (``ObjectSlot``) as that loop is recovered.
"""
from __future__ import annotations

CODE_SEG = 0x1030

# cs:[1] — the draw-mode/state flag the per-object draw reads to pick its screen
# stride/shift (>=3 => gameplay planar stride 0x28/shift 1; else 0x50/shift 2).
VAR_DRAW_MODE = 0x0001

# the multi-tile structure object table (record layout in the ledger).
OBJ_TABLE_OFF = 0x83EF
OBJ_SLOT_BYTES = 0x0A
OBJ_SLOTS = 0x0F


def read_draw_mode(mem) -> int:
    """The ``cs:[1]`` draw-mode flag (segment 1030)."""
    return mem.data[((CODE_SEG << 4) + VAR_DRAW_MODE) & 0xFFFFF]
