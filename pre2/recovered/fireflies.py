"""The firefly swarm draw (1030:54AB, the `0x6EA9` slot array).

A persistent 20-slot swarm (stride 8) that drifts toward a target point and is drawn as flickering
single pixels each frame. This is a SEPARATE effect system from the one-shot point particles
(`pre2.recovered.particles`, `[0x7DE6]` / 4B8E): the fireflies live across frames and are animated +
drawn by the same pass (54AB).

This module recovers the **draw** (the half the faithful renderer needs): given the post-update slot
positions, the camera, and the back page, it reproduces the exact firefly pixels the ASM writes. The
per-frame *animation* (the velocity/target flocking, RNG-driven) stays in the VM for now — the draw is
a pure function of the captured slot state, so the faithful renderer snapshots the slots at the 54AB
pass and replays only the plot.

Per-slot plot (cl=3 throughout, [asm 55A3]):
    sy = (y >> 3) - cam_row*16                      ; arithmetic shift
    if (sy & 0xffff) >= 0xB0: cull                  ; off top/bottom (unsigned)
    sx = (x >> 3) - cam_col*16
    if (sx & 0xffff) >= 0x140: cull                 ; off left/right
    off  = page + (sy & 0xff)*0x28 + (sx >> 3)
    bit  = 0x80 >> (sx & 7)
    plot bit in OR mode, color 14 if (timer&1)==0 else 15

The color is set by the EGA Set/Reset path [asm 5584]: on REAL hardware, when the slot timer (`[si+6]`)
is EVEN the GC forces plane 0 to 0 via Set/Reset (enable-set/reset=1) so the pixel is color **14**
(planes 1,2,3); when ODD all four planes take the CPU bit so the pixel is color **15** (white) — the
firefly flicker. Writes are OR (GC function-select 0x10, [asm 54BA]), so an existing pixel's bits kept.

IMPORTANT (faithful vs hardware): the dos_re EGA emulation (``memory._ega_wb``) does **not** implement
the Set/Reset / Enable-Set/Reset registers (GC index 0/1) — it writes the CPU data byte to every
map-masked plane. So under the VM oracle the even-timer color-14 path collapses and **every** firefly is
drawn color **15** (all four planes). The faithful renderer's oracle is the VM (what ``--view`` shows),
so this leaf OR's all four planes to match it byte-exact. The true 14/15 flicker is recovered in
``firefly_color(timer)`` for the ENHANCED renderer (which should restore the flicker), and is the right
fix-site if dos_re ever grows Set/Reset emulation.

The `0x55AA` sentinel in the first word marks a dead slot ([asm 54C2]); the bridge filters those out.
"""
from __future__ import annotations

from typing import Iterable, Sequence, Tuple

from pre2.islands import oracle_link

# (x, y, timer): x/y are the signed 16-bit fixed-point world positions ([si], [si+2]); timer is [si+6].
Firefly = Tuple[int, int, int]

_STRIDE = 0x28


@oracle_link("1030:54AB",
             "firefly swarm draw (the 0x6EA9 20-slot array, stride 8; draw half only): for each live "
             "slot (X!=0x55AA) compute the camera-relative screen pos sx=(X>>3)-cam_col*16, "
             "sy=(Y>>3)-cam_row*16, cull if unsigned sx>=0x140 or sy>=0xB0, then OR the pixel bit "
             "0x80>>(sx&7) at page+(sy&0xFF)*0x28+(sx>>3). dos_re has no Set/Reset emulation so the "
             "even/odd color-14/15 flicker collapses to color 15 (all 4 planes) -> OR all four to match "
             "the VM oracle byte-exact. The per-frame animation (RNG flocking) stays in the VM.",
             "VERIFIED", merge_target="render_frame")
def draw_fireflies(planes: Sequence[bytearray], slots: Iterable[Firefly], cam_col: int, cam_row: int,
                   page: int) -> None:
    """OR each live firefly's flicker pixel into ``planes`` (4 EGA planes) on ``page``."""
    cam_x = (cam_col << 4) & 0xFFFF      # cam_col*16 (shl cl=3, shl 1)
    cam_y = (cam_row << 4) & 0xFFFF
    for x, y, timer in slots:
        sy = ((_sar(y, 3) - cam_y) & 0xFFFF)
        if sy >= 0xB0:                   # cmp ax,0xb0 ; jae  (unsigned)
            continue
        sx = ((_sar(x, 3) - cam_x) & 0xFFFF)
        if sx >= 0x140:                  # cmp ax,0x140 ; jae
            continue
        off = (page + (sy & 0xFF) * _STRIDE + (sx >> 3)) & 0xFFFF
        bit = 0x80 >> (sx & 7)
        # dos_re has no Set/Reset emulation -> the CPU bit lands in every plane (color 15) regardless of
        # the timer parity; OR all four to match the VM oracle byte-exact (see module docstring).
        planes[0][off] |= bit
        planes[1][off] |= bit
        planes[2][off] |= bit
        planes[3][off] |= bit


def firefly_color(timer: int) -> int:
    """The real-hardware firefly color for a slot timer ([asm 5584]) — 14 (even) or 15 (odd).

    NOT used by the faithful draw (the VM oracle collapses both to 15); provided for the enhanced
    renderer, which should restore the flicker.
    """
    return 15 if (timer & 1) else 14


def _sar(v: int, n: int) -> int:
    """Arithmetic right shift of a 16-bit value (sign-extended)."""
    if v & 0x8000:
        v -= 0x10000
    return v >> n
