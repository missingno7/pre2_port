"""Prehistorik 2 point-particle draw (1030:4B8E) — recovered native primitive (pure).

The effect system draws short-lived **point particles** (spider-thread bits, fireflies, sparkles): up
to 20 slots at ``[0x7DE6]`` (6 bytes each: X word, Y word, angle byte, speed byte). Per frame the
producer spawns particles into the array; ``4B8E`` advances each by its angle/speed (sin/cos tables
``[0x6F90]``/``[0x7090]``), plots ONE white pixel (the bit set in all four planes via the EGA OR write),
and then **kills the slot** (``[si]=0xFFFF``) — so the particles are one-shot and gone by the per-frame
commit boundary.

This recovers the DRAW only — the position advance + the single-pixel plot — **read-only** (no kill /
writeback). The faithful renderer reproduces a frame's particles from a snapshot of the array taken at
``4B8E`` entry (before the engine consumes them); see :mod:`pre2.bridge.particles`.

Pure: no ``cpu``/``mem``. The angle/speed sin·cos tables are passed in as bytes (the bridge reads them).
"""
from __future__ import annotations

from pre2.islands import oracle_link

__all__ = ["PARTICLE_BASE", "PARTICLE_COUNT", "PARTICLE_STRIDE", "COS_TABLE", "SIN_TABLE",
           "DEAD_SLOT", "draw_particles", "advance_particle", "consume_particles"]

DEAD_SLOT = 0xFFFF         # the X-word sentinel 4B8E writes to kill a slot [asm 4C1D]

PARTICLE_BASE = 0x7DE6      # [0x7DE6] particle array
PARTICLE_COUNT = 0x14       # 20 slots (asm 4BA6 bp=0x14)
PARTICLE_STRIDE = 6         # 6 bytes/slot (asm 4C21 add si,6): X.w Y.w angle.b speed.b
COS_TABLE = 0x6F90          # [0x6F90] signed cos table (X velocity), indexed by angle
SIN_TABLE = 0x7090          # [0x7090] signed sin table (Y velocity)

_VIEW_W = 0x140             # 320 — off-screen X cull (asm 4C00 cmp 0x140)
_VIEW_H = 0xB0              # 176 — off-screen Y cull (asm 4BEB cmp 0xb0)
_ROW = 0x28


def _s8(v):
    v &= 0xFF
    return v - 256 if v & 0x80 else v


@oracle_link("1030:4B8E",
             "point-particle draw: for each active slot (X!=0xFFFF) advance X by "
             "((s8(cos[angle])>>2)*s8(speed))>>4 and Y by the sin equivalent, then if the camera-"
             "relative screen pos is on-screen (X<0x140, Y<0xB0) plot one white pixel (bit 0x80>>(x&7) "
             "OR'd into all 4 planes) at page+y*0x28+(x>>3). Read-only (the ASM also kills the slot).",
             "VERIFIED", merge_target="render_frame")
def draw_particles(planes, particles, cam_col, cam_row, y_bias, page, cos_table, sin_table):
    """Recover ``1030:4B8E`` (draw only). ``particles`` = the active slots ``(x, y, angle, speed)``
    snapshotted at 4B8E entry; ``cam_col``/``cam_row`` the tile camera (``[0x2DE4]``/``[0x2DE6]``,
    shifted to pixels here); ``y_bias`` = ``[0x6BC4]``; ``page`` the target EGA page offset; the
    tables are 256-byte signed sin/cos slices. Plots each particle's pixel into ``planes`` (OR)."""
    cam_x = (cam_col << 4) & 0xFFFF
    cam_y = (cam_row << 4) & 0xFFFF
    yb = _s8(y_bias)
    page &= 0xFFFF
    for (x, y, angle, speed) in particles:
        nx, ny = advance_particle(x, y, angle, speed, cos_table, sin_table)
        _plot_particle(planes, nx, ny, cam_x, cam_y, yb, page)


def advance_particle(x, y, angle, speed, cos_table, sin_table):
    """Advance one particle by its angle/speed [asm 4BB0-4BD2]. Returns ``(nx, ny)`` (wrapped 16-bit).

    ``nx = x + ((s8(cos[angle])>>2) * s8(speed)) >> 4`` (all arithmetic shifts), ``ny`` the sin equivalent."""
    sp = _s8(speed)
    nx = (x + (((_s8(cos_table[angle & 0xFF]) >> 2) * sp) >> 4)) & 0xFFFF        # [asm 4BB0-4BC0]
    ny = (y + (((_s8(sin_table[angle & 0xFF]) >> 2) * sp) >> 4)) & 0xFFFF        # [asm 4BC2-4BD2]
    return nx, ny


def _plot_particle(planes, nx, ny, cam_x, cam_y, yb, page):
    """Plot one advanced particle's white pixel if on-screen [asm 4BD5-4C1A]."""
    sy = (ny - yb - cam_y) & 0xFFFF                                              # [asm 4BDA-4BE9]
    if sy >= _VIEW_H:                                                            # [asm 4BEB jae]
        return
    sx = (nx - cam_x) & 0xFFFF                                                   # [asm 4BF6-4BFE]
    if sx >= _VIEW_W:                                                            # [asm 4C00 jae]
        return
    off = (page + sy * _ROW + (sx >> 3)) & 0xFFFF                                # [asm 4BF0-4C12]
    bit = 0x80 >> (sx & 7)                                                       # [asm 4C16]
    for p in range(4):                                                           # [asm 4C1A xchg, OR all planes]
        planes[p][off] |= bit


@oracle_link("1030:4B8E",
             "the FULL 4B8E pass (the live-replacement contract, vs draw_particles' draw-only): for each "
             "active slot advance + plot (as draw_particles) AND write the advanced Y back to [slot+2] then "
             "kill the slot ([slot]=0xFFFF). Returns the per-slot writeback the bridge applies to the array.",
             "VERIFIED", merge_target="render_frame")
def consume_particles(planes, slots, cam_col, cam_row, y_bias, page, cos_table, sin_table):
    """The full 4B8E semantics for the live replacement. ``slots`` = ``[(index, x, y, angle, speed)]`` for
    every active slot (X != 0xFFFF). Plots into ``planes`` (OR) and returns ``[(index, ny)]`` — the bridge
    writes ``[slot+2]=ny`` and ``[slot]=DEAD_SLOT`` (the advanced-X write at 4BC0 is overwritten by the
    4C1D kill, so only Y persists)."""
    cam_x = (cam_col << 4) & 0xFFFF
    cam_y = (cam_row << 4) & 0xFFFF
    yb = _s8(y_bias)
    page &= 0xFFFF
    writeback = []
    for (index, x, y, angle, speed) in slots:
        nx, ny = advance_particle(x, y, angle, speed, cos_table, sin_table)
        _plot_particle(planes, nx, ny, cam_x, cam_y, yb, page)
        writeback.append((index, ny))
    return writeback
