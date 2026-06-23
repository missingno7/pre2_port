"""VM↔memory layout for the end-level circular-iris transition (1030:31F4..32B0).

Layout only — it reads the per-frame iris inputs from DGROUP (radius, player centre,
clamp, page, the quarter-circle cos/sin tables) and writes the scaled-column tables +
the cleared EGA planes back. The geometry/clear *math* lives in
``pre2.recovered.transition`` (``build_scaled_columns`` + ``draw_scale_frame``).

The block is inline (not a CALL): 31F4 builds this frame's iris-circle column table,
324B clears everything outside the circle, falling through to 32B0. The controller after
32B0 only reads ``[0x2DC2]/[0x2DC0]/[0x2DD0]`` and re-renders + fades — none of the block's
scratch (``[0x2DCC]/[0x2DCE]/[0x2DD2]/[0x2DCA]``, si/bp) — so the planes are the contract.
"""
from __future__ import annotations

from dataclasses import dataclass

from pre2.bridge.object_render import read_planes  # noqa: F401 — re-export
from pre2.bridge.sprites import plane_views  # noqa: F401 — re-export (writable VRAM views)

_DS = 0x1A0F                # DGROUP segment (GOG build)
_RADIUS = 0x2DD0           # [asm 320C: mul byte] iris radius (low byte; shrinks each frame)
_X_OFF = 0x2DC6            # [asm 3214] player X (circle centre)
_Y_OFF = 0x2DC8            # [asm 3235] player Y (circle centre)
_X_CLAMP = 0x2DC4          # [asm 321E/3253] X clamp + draw start column
_PAGE = 0x2DD8             # destination CRTC page
_COS_T = 0x7090            # [asm 3208] quarter-circle cos table (1 byte/col)
_SIN_T = 0x6F90            # [asm 322B] quarter-circle sin table
_TBL_X = 0x6B14            # [asm 3227] scaled-X column table (words)
_TBL_Y = 0x6A88            # [asm 3239] scaled-Y column table (words)
_COLS = 0x41               # SCALE_COLUMNS scanned per frame
_RUNNING = 0x2DCC         # [asm 3224] build's running min-X (ends at last kept X)
_COLCNT = 0x2DCE          # [asm 3240/3244] build column counter (ends at 0x41)
_CUR_Y = 0x2DCA           # [asm 3266] draw's current row (terminal scratch)
_CUR_X = 0x2DD2           # [asm 32A8] draw's current column (terminal scratch)
_RUN_INIT = 0x7D0         # [asm 31F6] running seed when no column is kept


@dataclass(frozen=True)
class IrisInputs:
    """One frame's iris inputs: the radius, circle centre, clamp, page, the cos/sin
    source tables, and the current (stale) scaled-column tables (build overwrites the
    first ``count`` of these; the tail is read as-is by the clear pass, like the ASM)."""
    scale: int
    x_off: int
    y_off: int
    x_clamp: int
    page: int
    src_x: list
    src_y: list
    tbl_x: list
    tbl_y: list


def _rb(mem, off: int) -> int:
    return mem.data[(_DS << 4) + off]


def _rw(mem, off: int) -> int:
    b = (_DS << 4) + off
    return mem.data[b] | (mem.data[b + 1] << 8)


def _rws(mem, off: int) -> int:
    v = _rw(mem, off)
    return v - 0x10000 if v & 0x8000 else v


def _ww(mem, off: int, val: int) -> None:
    b = (_DS << 4) + off
    mem.data[b] = val & 0xFF
    mem.data[b + 1] = (val >> 8) & 0xFF


def read_iris_inputs(mem) -> IrisInputs:
    """Read the per-frame iris inputs at the block entry (1030:31F4)."""
    base = _DS << 4
    return IrisInputs(
        scale=_rb(mem, _RADIUS),
        x_off=_rws(mem, _X_OFF),
        y_off=_rws(mem, _Y_OFF),
        x_clamp=_rws(mem, _X_CLAMP),
        page=_rw(mem, _PAGE),
        src_x=list(mem.data[base + _COS_T:base + _COS_T + _COLS]),
        src_y=list(mem.data[base + _SIN_T:base + _SIN_T + _COLS]),
        tbl_x=[_rws(mem, _TBL_X + 2 * i) for i in range(_COLS)],
        tbl_y=[_rws(mem, _TBL_Y + 2 * i) for i in range(_COLS)],
    )


def write_tables(mem, xs, ys) -> None:
    """Write the recovered scaled-column tables back to ``[0x6B14]``/``[0x6A88]`` (the
    first ``len(xs)`` words), matching what the ASM build loop leaves in DGROUP."""
    for i, v in enumerate(xs):
        _ww(mem, _TBL_X + 2 * i, v)
    for i, v in enumerate(ys):
        _ww(mem, _TBL_Y + 2 * i, v)


def write_scratch(mem, xs, cur_y, cur_x) -> None:
    """Write the DGROUP scratch the ASM block leaves at 32B0, so the persistent-memory
    state matches the ASM byte-for-byte: ``[0x2DCC]`` = running min-X (last kept column,
    else the 0x7D0 seed), ``[0x2DCE]`` = 0x41 (build counter), ``[0x2DCA]``/``[0x2DD2]``
    = the draw loop's terminal row/column. (These are re-initialised before each use, so
    they are not read stale — written only to keep the whole-memory oracle exact.)"""
    _ww(mem, _RUNNING, xs[-1] if xs else _RUN_INIT)
    _ww(mem, _COLCNT, _COLS)
    _ww(mem, _CUR_Y, cur_y)
    _ww(mem, _CUR_X, cur_x)
