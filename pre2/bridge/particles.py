"""Read the point-particle frame (1030:4B8E inputs) from VM memory.

The particles are one-shot: ``4B8E`` draws then kills each slot, so the array is empty by the per-frame
commit boundary (6772). The faithful renderer must therefore snapshot the array at ``4B8E`` ENTRY (the
caller hooks that CS:IP). This bridge does the layout-only extraction into a plain
:class:`ParticleFrame`; the recovered :func:`pre2.recovered.particles.draw_particles` consumes it.
"""
from __future__ import annotations

from dataclasses import dataclass

from pre2.recovered.particles import (COS_TABLE, PARTICLE_BASE, PARTICLE_COUNT, PARTICLE_STRIDE,
                                       SIN_TABLE)

_DS = 0x1A0F
_CAM_COL = 0x2DE4    # [0x2DE4] tile camera column
_CAM_ROW = 0x2DE6    # [0x2DE6] tile camera row
_YBIAS = 0x6BC4      # [0x6BC4] vertical bias subtracted from the particle Y


def _rw(mem, off):
    b = ((_DS << 4) + off) & 0xFFFFF
    return mem.data[b] | (mem.data[b + 1] << 8)


def _rb(mem, off):
    return mem.data[((_DS << 4) + off) & 0xFFFFF]


@dataclass(frozen=True)
class ParticleFrame:
    """The active particles + the inputs ``draw_particles`` needs, snapshotted at 4B8E entry."""
    particles: tuple        # tuple[(x, y, angle, speed)] for the active slots (X != 0xFFFF)
    cam_col: int
    cam_row: int
    y_bias: int
    cos: bytes              # 256-byte signed cos slice ([0x6F90])
    sin: bytes              # 256-byte signed sin slice ([0x7090])


def read_particles(mem) -> ParticleFrame:
    """Snapshot the active particle slots + camera/bias + the sin·cos tables. Call at 1030:4B8E entry
    (before the engine advances/kills the slots)."""
    parts = []
    for k in range(PARTICLE_COUNT):
        b = PARTICLE_BASE + k * PARTICLE_STRIDE
        x = _rw(mem, b)
        if x == 0xFFFF:                       # inactive slot sentinel
            continue
        parts.append((x, _rw(mem, b + 2), _rb(mem, b + 4), _rb(mem, b + 5)))
    base = (_DS << 4)
    return ParticleFrame(
        particles=tuple(parts),
        cam_col=_rw(mem, _CAM_COL),
        cam_row=_rw(mem, _CAM_ROW),
        y_bias=_rb(mem, _YBIAS),
        cos=bytes(mem.data[base + COS_TABLE:base + COS_TABLE + 256]),
        sin=bytes(mem.data[base + SIN_TABLE:base + SIN_TABLE + 256]),
    )
