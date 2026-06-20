"""Checkpoint for the frame renderer's tile-row draw (1030:346E).

Recovered logic: ``pre2.recovered.frame_renderer.draw_tile_row``; data model:
``pre2.bridge.frame``. Merge target: the frame renderer.

Per the island-composition rule, the recovered ``draw_tile_row`` calls the recovered
``blit_sprite`` directly (no ASM contact point inside the row). This adapter just
bridges VM state in/out: it reads the camera/scroll inputs + the level TileMap, runs
the recovered row draw on the live planes, and writes back the OR-accumulated flags.

Contract (verified vs ASM by pre2/probes/verify_frame.py): the four A000 planes for
one 20-tile row, the OR-accumulated flag bytes [0x6BB9]/[0x2DEE]/[0x2DF0], and that
``di`` (and the other pushed registers) are preserved.
"""

from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from pre2.bridge import frame as _frame
from pre2.bridge import sprites as _spr
from pre2.recovered.frame_renderer import RowFlags, draw_grid, draw_tile_row, panel_copy, scroll_copy

from .common import _DATA_SEG, report

_ROW_ENTRY = (0x1030, 0x346E)
_ROW_EXIT = (0x1030, 0x34EC)   # the routine's near RET
_GRID_ENTRY = (0x1030, 0x3582)
_GRID_EXIT = (0x1030, 0x3645)  # the grid redraw's near RET
_SCROLL_ENTRY = (0x1030, 0x3A08)
_SCROLL_EXIT = (0x1030, 0x3AD2)  # the scroll-copy's near RET
_PANEL_ENTRY = (0x1030, 0x3035)
_PANEL_EXIT = (0x1030, 0x307B)   # the page-flip copy's near RET
_VAR_DEST_PAGE = 0x2DD4          # back page (scroll/panel source)
_VAR_FRONT_PAGE = 0x2DD2         # front page (panel dest)
_VAR_ROW_RING = 0x2DE6
_VAR_ROW_FACTOR = 0x6BF4
_VAR_PLANE_ATTR = 0x6BB9
_VAR_TILE_FLAGS = 0x2DEE
_VAR_TILE_TYPE = 0x2DF0
_VAR_DIRTY_ROWS = 0x2DF1
_VAR_PREV_X = 0x2DDC
_VAR_PREV_Y = 0x2DDE
_VAR_CAMERA_X = 0x2DE0
_VAR_CAMERA_Y = 0x2DE2
_VAR_SCROLL_SRC = 0x2DB6
_VAR_COL_RING = 0x2DE4
_VAR_FINE_SCROLL = 0x6BC0


def _rb(mem, off):
    return mem.data[((_DATA_SEG << 4) + off) & 0xFFFFF]


def _wb(mem, off, val):
    mem.data[((_DATA_SEG << 4) + off) & 0xFFFFF] = val & 0xFF


def _inputs(cpu):
    """Read everything ``346E`` consumes from VM state into recovered-domain values."""
    mem = cpu.mem
    return {
        "tile_offset": cpu.s.ax & 0xFFFF,
        "di": cpu.s.di & 0xFFFF,
        "scroll_src": mem.rw(_DATA_SEG, _VAR_SCROLL_SRC),
        "col_ring": _rb(mem, _VAR_COL_RING),
        "fine_scroll": _rb(mem, _VAR_FINE_SCROLL),
        "tilemap": _frame.read_tilemap(mem),
        "blit_type": _frame.read_blit_type_table(mem),
        "mask_region": _frame.read_mask_region(mem),
        "seed": RowFlags(_rb(mem, _VAR_PLANE_ATTR), _rb(mem, _VAR_TILE_FLAGS), _rb(mem, _VAR_TILE_TYPE)),
    }


def _run(planes, a) -> tuple[int, RowFlags]:
    return draw_tile_row(
        planes, a["tilemap"], a["tile_offset"], a["di"], a["scroll_src"],
        a["col_ring"], a["fine_scroll"], a["blit_type"], a["mask_region"], a["seed"],
    )


def _write_flags(mem, flags: RowFlags) -> None:
    base = (_DATA_SEG << 4) & 0xFFFFF
    mem.data[base + _VAR_PLANE_ATTR] = flags.plane_attr & 0xFF
    mem.data[base + _VAR_TILE_FLAGS] = flags.tile_flags & 0xFF
    mem.data[base + _VAR_TILE_TYPE] = flags.tile_type & 0xFF


def _grid_inputs(cpu) -> dict:
    """Read everything ``3582`` consumes from VM state."""
    mem = cpu.mem
    return {
        "camera_x": mem.rw(_DATA_SEG, _VAR_CAMERA_X),
        "camera_y": mem.rw(_DATA_SEG, _VAR_CAMERA_Y),
        "prev_x": mem.rw(_DATA_SEG, _VAR_PREV_X),
        "prev_y": mem.rw(_DATA_SEG, _VAR_PREV_Y),
        "dirty": _rb(mem, _VAR_TILE_TYPE),         # [0x2DF0]
        "dirty_rows": _rb(mem, _VAR_DIRTY_ROWS),   # [0x2DF1]
        "scroll_src": mem.rw(_DATA_SEG, _VAR_SCROLL_SRC),
        "col_ring": mem.rw(_DATA_SEG, _VAR_COL_RING),
        "fine_scroll": _rb(mem, _VAR_FINE_SCROLL),
        "tilemap": _frame.read_tilemap(mem),
        "blit_type": _frame.read_blit_type_table(mem),
        "mask_region": _frame.read_mask_region(mem),
    }


def _run_grid(planes, g):
    return draw_grid(planes, g["tilemap"], g["camera_x"], g["camera_y"], g["prev_x"], g["prev_y"],
                     g["dirty"], g["dirty_rows"], g["scroll_src"], g["col_ring"], g["fine_scroll"],
                     g["blit_type"], g["mask_region"])


def _write_grid_result(mem, res) -> None:
    mem.ww(_DATA_SEG, _VAR_PREV_X, res.prev_x & 0xFFFF)   # prev always written (3590/dirty_rows!=0: no-op)
    mem.ww(_DATA_SEG, _VAR_PREV_Y, res.prev_y & 0xFFFF)
    if res.redrew:                                        # flags reset+accumulated only on redraw
        _wb(mem, _VAR_TILE_FLAGS, res.tile_flags)
        _wb(mem, _VAR_TILE_TYPE, res.dirty)
        _wb(mem, _VAR_DIRTY_ROWS, res.dirty_rows)


@registry.replace(*_GRID_ENTRY, "frame_grid")
def frame_grid(cpu) -> None:
    """Native replacement for the visible-grid redraw at 1030:3582."""
    mem = cpu.mem
    g = _grid_inputs(cpu)

    if getattr(cpu, "pre2_verify_mode", False):
        snap = _spr.snapshot_planes(mem)
        res = _run_grid(snap, g)
        cpu.pre2_frame_grid_pending.append((dict(g), snap, res))
        interpret_current_instruction_without_hook(cpu)
        return

    res = _run_grid(_spr.plane_views(mem), g)
    _write_grid_result(mem, res)
    cpu.s.ip = cpu.pop()  # near ret; di and the other pushed regs are preserved


def _scroll_inputs(cpu) -> dict:
    mem = cpu.mem
    return {
        "scroll_src": mem.rw(_DATA_SEG, _VAR_SCROLL_SRC),
        "dest": mem.rw(_DATA_SEG, _VAR_DEST_PAGE),
        "col_ring": _rb(mem, _VAR_COL_RING),
        "fine_scroll": _rb(mem, _VAR_FINE_SCROLL),
        "row_ring": mem.rw(_DATA_SEG, _VAR_ROW_RING),
        "row_factor": mem.rw(_DATA_SEG, _VAR_ROW_FACTOR),
    }


@registry.replace(*_SCROLL_ENTRY, "frame_scroll_copy")
def frame_scroll_copy(cpu) -> None:
    """Native replacement for the vertical-scroll screen copy at 1030:3A08."""
    mem = cpu.mem
    g = _scroll_inputs(cpu)

    if getattr(cpu, "pre2_verify_mode", False):
        snap = _spr.snapshot_planes(mem)
        scroll_copy(snap, **g)
        cpu.pre2_frame_scroll_pending.append(snap)
        interpret_current_instruction_without_hook(cpu)
        return

    scroll_copy(_spr.plane_views(mem), **g)
    cpu.s.ip = cpu.pop()  # near ret; bx/di/si/ds/es preserved


@registry.replace(*_PANEL_ENTRY, "frame_panel_copy")
def frame_panel_copy(cpu) -> None:
    """Native replacement for the double-buffer page-flip copy at 1030:3035."""
    mem = cpu.mem
    src = mem.rw(_DATA_SEG, _VAR_DEST_PAGE)
    dst = mem.rw(_DATA_SEG, _VAR_FRONT_PAGE)

    if getattr(cpu, "pre2_verify_mode", False):
        snap = _spr.snapshot_planes(mem)
        panel_copy(snap, src, dst)
        cpu.pre2_frame_panel_pending.append(snap)
        interpret_current_instruction_without_hook(cpu)
        return

    panel_copy(_spr.plane_views(mem), src, dst)
    cpu.s.ip = cpu.pop()  # near ret; regs preserved (vsync wait omitted, timing-only)


# NOTE on 1030:3B40 (the frame compositor): it is a static composition —
# sti; [0x2DF0]=1; [0x2DDC]=0x55AA; call 3582; call 3A08; call 3035; pop es; pop ds;
# ret — i.e. draw_grid -> scroll_copy -> panel_copy over the now-native leaves. We do
# NOT wire it as a native replacement: no available demo reaches 3B40 (its leaves are
# exercised via their other callers: 0237 / 01E2 / 023A), so a native 3B40 cannot be
# verified yet. The hybrid runtime already runs the three leaves natively when the ASM
# 3B40 calls them; wire a native compositor only once a scenario exercises 3B40 so it
# can be lockstep-verified (the call order itself is static: 3B4C/3B4F/3B52).


@registry.replace(*_ROW_ENTRY, "frame_tile_row")
def frame_tile_row(cpu) -> None:
    """Native replacement for the tile-row draw at 1030:346E."""
    mem = cpu.mem
    a = _inputs(cpu)

    if getattr(cpu, "pre2_verify_mode", False):
        snap = _spr.snapshot_planes(mem)
        _di, flags = _run(snap, a)
        cpu.pre2_frame_pending.append((a["di"], snap, flags))
        interpret_current_instruction_without_hook(cpu)
        return

    _di, flags = _run(_spr.plane_views(mem), a)
    _write_flags(mem, flags)
    cpu.s.ip = cpu.pop()  # near ret; di and the other pushed regs are preserved


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Install the lockstep verify-exit hook at the row draw's RET."""

    def _verify_at_exit(c) -> None:
        if c.pre2_frame_pending:
            entry_di, snap, flags = c.pre2_frame_pending.pop()
            mem = c.mem
            reason = None
            live = _spr.snapshot_planes(mem)
            for p in range(4):
                if bytes(live[p]) != bytes(snap[p]):
                    reason = f"plane {p}"
                    break
            if reason is None and (c.s.di & 0xFFFF) != entry_di:
                reason = f"di not preserved {c.s.di & 0xFFFF:04X}!={entry_di:04X}"
            if reason is None:
                for name, off, val in (("plane_attr", _VAR_PLANE_ATTR, flags.plane_attr & 0xFF),
                                       ("tile_flags", _VAR_TILE_FLAGS, flags.tile_flags & 0xFF),
                                       ("tile_type", _VAR_TILE_TYPE, flags.tile_type & 0xFF)):
                    if _rb(mem, off) != val:
                        reason = f"{name} {_rb(mem, off):02X}!={val:02X}"
                        break
            report(stats, on_result, raise_on_divergence, "frame_tile_row", reason)
        interpret_current_instruction_without_hook(c)  # original near-ret

    cpu.replacement_hooks[_ROW_EXIT] = _verify_at_exit
    cpu.hook_names[_ROW_EXIT] = "frame_tile_row_verify"

    def _grid_verify_at_exit(c) -> None:
        if c.pre2_frame_grid_pending:
            g, snap, res = c.pre2_frame_grid_pending.pop()
            mem = c.mem
            reason = None
            if res.redrew:
                live = _spr.snapshot_planes(mem)
                for p in range(4):
                    if bytes(live[p]) != bytes(snap[p]):
                        reason = f"plane {p}"
                        break
            checks = [("[0x2DDC]", _VAR_PREV_X, res.prev_x & 0xFFFF, True),
                      ("[0x2DDE]", _VAR_PREV_Y, res.prev_y & 0xFFFF, True)]
            if res.redrew:
                checks += [("[0x2DEE]", _VAR_TILE_FLAGS, res.tile_flags & 0xFF, False),
                           ("[0x2DF0]", _VAR_TILE_TYPE, res.dirty & 0xFF, False),
                           ("[0x2DF1]", _VAR_DIRTY_ROWS, res.dirty_rows & 0xFF, False)]
            if reason is None:
                for name, off, val, is_word in checks:
                    actual = mem.rw(_DATA_SEG, off) if is_word else _rb(mem, off)
                    if actual != val:
                        reason = f"{name} asm={actual:X} rec={val:X}"
                        break
            report(stats, on_result, raise_on_divergence, "frame_grid", reason)
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[_GRID_EXIT] = _grid_verify_at_exit
    cpu.hook_names[_GRID_EXIT] = "frame_grid_verify"

    def _scroll_verify_at_exit(c) -> None:
        if c.pre2_frame_scroll_pending:
            snap = c.pre2_frame_scroll_pending.pop()
            mem = c.mem
            reason = None
            live = _spr.snapshot_planes(mem)
            for p in range(4):
                if bytes(live[p]) != bytes(snap[p]):
                    reason = f"plane {p}"
                    break
            report(stats, on_result, raise_on_divergence, "frame_scroll_copy", reason)
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[_SCROLL_EXIT] = _scroll_verify_at_exit
    cpu.hook_names[_SCROLL_EXIT] = "frame_scroll_copy_verify"

    def _panel_verify_at_exit(c) -> None:
        if c.pre2_frame_panel_pending:
            snap = c.pre2_frame_panel_pending.pop()
            mem = c.mem
            reason = None
            live = _spr.snapshot_planes(mem)
            for p in range(4):
                if bytes(live[p]) != bytes(snap[p]):
                    reason = f"plane {p}"
                    break
            report(stats, on_result, raise_on_divergence, "frame_panel_copy", reason)
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[_PANEL_EXIT] = _panel_verify_at_exit
    cpu.hook_names[_PANEL_EXIT] = "frame_panel_copy_verify"
