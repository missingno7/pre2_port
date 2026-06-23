"""Prehistorik 2 camera-shake APPLY — recovered native controller (pure).

On a fall/landing the engine shakes the gameplay viewport vertically. The shake magnitude lives in
``[0x6BEA]`` (set 7/4 on landing, decayed elsewhere by the group-decay 5A4A); once per frame the
APPLY routine ``1030:4C30`` converts it into the **renderer-visible** vertical offset ``[0x6BF8]``
(== :attr:`RendererState.row_factor`, which the grid/scroll/object passes already consume). So this
is the controller that *produces* the row-stride bias the renderer *consumes*.

Capstone-confirmed ``1030:4C30..4C68`` (a clean CALL'd routine):

    cmp [6BEA],1; jb ret            ; magnitude 0   -> leave [6BF8] unchanged (no write)
    je  [6BF8]=0                    ; magnitude 1   -> [6BF8]=0
    ; magnitude > 1:
    test [6BD5],1; je [6BF8]=0      ; EVEN frame    -> [6BF8]=0
    ; ODD frame:
    if [4F27] not in {5,0x20}: [4F1E]-=3   ; the SEPARATE small horizontal nudge
    inc [6BEA]                              ; +1 jitter (read AFTER the inc)
    [6BF8] = [6BEA]                         ; -> magnitude+1

So the vertical jolt alternates ``{0, magnitude+1}`` by frame parity; the ``[0x4F1E]-=3`` is a
distinct horizontal nudge (not the vertical shake). Pure: no ``cpu``/``mem`` imports; the VM↔memory
translation lives in ``pre2/bridge`` / the checkpoint.
"""
from __future__ import annotations

from dataclasses import dataclass

from pre2.islands import oracle_link

__all__ = ["ShakeApply", "H_NUDGE", "H_NUDGE_SKIP", "apply_camera_shake"]

H_NUDGE = 3                 # [asm 4C50: sub [0x4F1E],3] the horizontal nudge magnitude
H_NUDGE_SKIP = (5, 0x20)    # [asm 4C42/4C49: cmp [0x4F27],5 / ,0x20] states that skip the nudge


@dataclass(frozen=True)
class ShakeApply:
    """The full write contract of one ``4C30`` apply: the renderer-visible row-stride bias
    ``row_factor`` ([0x6BF8]), the (jitter-updated) ``magnitude`` ([0x6BEA]), and the horizontal
    scroll var ``h_scroll`` ([0x4F1E])."""
    row_factor: int    # [0x6BF8] vertical jolt fed to the renderer (0 or magnitude+1)
    magnitude: int     # [0x6BEA] after the odd-frame +1 jitter
    h_scroll: int      # [0x4F1E] after the conditional -3 horizontal nudge


@oracle_link("1030:4C30",
             "camera-shake apply: from magnitude [0x6BEA] + frame parity [0x6BD5]&1, write the "
             "renderer row-stride bias [0x6BF8] (0 on even / magnitude+1 on odd, after the +1 "
             "jitter), and on odd frames nudge [0x4F1E]-=3 unless [0x4F27] in {5,0x20}. mag 0 "
             "leaves [0x6BF8] unchanged; mag 1 writes 0.",
             "OBSERVED", merge_target="render_frame")
def apply_camera_shake(row_factor_in: int, magnitude: int, parity: int,
                       f27: int, h_scroll_in: int) -> ShakeApply:
    """Recover ``1030:4C30..4C68`` — one frame's shake apply. Inputs are the values at routine
    entry: ``row_factor_in``=[0x6BF8], ``magnitude``=[0x6BEA], ``parity``=[0x6BD5] (bit 0 tested),
    ``f27``=[0x4F27], ``h_scroll_in``=[0x4F1E]. Returns the resulting :class:`ShakeApply`."""
    magnitude &= 0xFF
    rf_in = row_factor_in & 0xFFFF
    h_in = h_scroll_in & 0xFFFF
    if magnitude == 0:                                   # [asm 4C35 jb 4C68] ret, no writes
        return ShakeApply(rf_in, magnitude, h_in)
    if magnitude == 1:                                   # [asm 4C37 je 4C63] [0x6BF8]=0
        return ShakeApply(0, magnitude, h_in)
    if (parity & 1) == 0:                                # [asm 4C3B/4C40] EVEN -> [0x6BF8]=0
        return ShakeApply(0, magnitude, h_in)
    h = h_in
    if f27 not in H_NUDGE_SKIP:                          # [asm 4C42..4C50] odd -> horizontal nudge
        h = (h_in - H_NUDGE) & 0xFFFF
    mag = (magnitude + 1) & 0xFF                         # [asm 4C55] inc [0x6BEA] (the +1 jitter)
    return ShakeApply(mag, mag, h)                       # [asm 4C59/4C5E] [0x6BF8]=[0x6BEA]
