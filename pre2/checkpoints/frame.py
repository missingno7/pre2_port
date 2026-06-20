"""Checkpoints for the frame renderer (1030:346E / 3582 / 3A08 / 3035).

Thin VM contact points only: each adapter reads the original VM state **through the
bridge** (``pre2.bridge.frame`` Camera/ScrollState/TileMap dataclasses + readers —
the bridge owns every segment:offset), calls the recovered renderer function, writes
the contract back through the bridge, and returns to original flow. No renderer logic
and no raw memory offsets live here; that all lives in ``pre2/recovered`` and
``pre2/bridge``. Verify mode diffs the recovered result against the ASM at each RET.
"""

from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from pre2.bridge import frame as _frame
from pre2.bridge import sprites as _spr
from pre2.recovered.frame_renderer import RowFlags, draw_grid, draw_tile_row, panel_copy, scroll_copy

from .common import report

_ROW_ENTRY = (0x1030, 0x346E)
_ROW_EXIT = (0x1030, 0x34EC)     # tile-row draw near RET
_GRID_ENTRY = (0x1030, 0x3582)
_GRID_EXIT = (0x1030, 0x3645)    # grid redraw near RET
_SCROLL_ENTRY = (0x1030, 0x3A08)
_SCROLL_EXIT = (0x1030, 0x3AD2)  # scroll-copy near RET
_PANEL_ENTRY = (0x1030, 0x3035)
_PANEL_EXIT = (0x1030, 0x307B)   # page-flip copy near RET


# ---- tile-row draw (346E) ---------------------------------------------------
def _run_row(cpu, planes):
    mem = cpu.mem
    st = _frame.read_scroll_state(mem)
    return draw_tile_row(
        planes, _frame.read_tilemap(mem),
        cpu.s.ax & 0xFFFF, cpu.s.di & 0xFFFF,        # tile_offset, di — register inputs
        st.scroll_src, st.camera.col_ring, st.camera.fine_scroll,
        _frame.read_blit_type_table(mem), _frame.read_mask_region(mem),
        RowFlags(*_frame.read_row_flags(mem)),       # seed = current accumulators
    )


@registry.replace(*_ROW_ENTRY, "frame_tile_row")
def frame_tile_row(cpu) -> None:
    """Native replacement for the tile-row draw at 1030:346E."""
    mem = cpu.mem
    if getattr(cpu, "pre2_verify_mode", False):
        snap = _spr.snapshot_planes(mem)
        _di, flags = _run_row(cpu, snap)
        cpu.pre2_frame_pending.append((cpu.s.di & 0xFFFF, snap, flags))
        interpret_current_instruction_without_hook(cpu)
        return
    _di, flags = _run_row(cpu, _spr.plane_views(mem))
    _frame.write_row_flags(mem, flags.plane_attr, flags.tile_flags, flags.tile_type)
    cpu.s.ip = cpu.pop()  # near ret; di and the other pushed regs are preserved


# ---- grid redraw (3582) -----------------------------------------------------
def _run_grid(cpu, planes):
    mem = cpu.mem
    st = _frame.read_scroll_state(mem)
    c = st.camera
    return draw_grid(
        planes, _frame.read_tilemap(mem), c.x, c.y, c.prev_x, c.prev_y,
        st.dirty, st.dirty_rows, st.scroll_src, c.col_ring, c.fine_scroll,
        _frame.read_blit_type_table(mem), _frame.read_mask_region(mem),
    )


@registry.replace(*_GRID_ENTRY, "frame_grid")
def frame_grid(cpu) -> None:
    """Native replacement for the visible-grid redraw at 1030:3582."""
    mem = cpu.mem
    if getattr(cpu, "pre2_verify_mode", False):
        snap = _spr.snapshot_planes(mem)
        res = _run_grid(cpu, snap)
        cpu.pre2_frame_grid_pending.append((_frame.read_camera(mem), snap, res))
        interpret_current_instruction_without_hook(cpu)
        return
    res = _run_grid(cpu, _spr.plane_views(mem))
    _frame.write_dirty_state(
        mem, res.prev_x, res.prev_y,
        dirty=res.dirty if res.redrew else None,
        dirty_rows=res.dirty_rows if res.redrew else None,
        tile_flags=res.tile_flags if res.redrew else None,
    )
    cpu.s.ip = cpu.pop()  # near ret; di/regs preserved


# ---- scroll-copy (3A08) -----------------------------------------------------
def _run_scroll(cpu, planes):
    st = _frame.read_scroll_state(cpu.mem)
    c = st.camera
    scroll_copy(planes, st.scroll_src, st.dest_page_b, c.col_ring,
                c.fine_scroll, c.row_ring, st.row_factor)


@registry.replace(*_SCROLL_ENTRY, "frame_scroll_copy")
def frame_scroll_copy(cpu) -> None:
    """Native replacement for the vertical-scroll screen copy at 1030:3A08."""
    mem = cpu.mem
    if getattr(cpu, "pre2_verify_mode", False):
        snap = _spr.snapshot_planes(mem)
        _run_scroll(cpu, snap)
        cpu.pre2_frame_scroll_pending.append(snap)
        interpret_current_instruction_without_hook(cpu)
        return
    _run_scroll(cpu, _spr.plane_views(mem))
    cpu.s.ip = cpu.pop()  # near ret; bx/di/si/ds/es preserved


# ---- page-flip copy (3035) --------------------------------------------------
def _run_panel(cpu, planes):
    st = _frame.read_scroll_state(cpu.mem)
    panel_copy(planes, st.dest_page_b, st.dest_page_a)


@registry.replace(*_PANEL_ENTRY, "frame_panel_copy")
def frame_panel_copy(cpu) -> None:
    """Native replacement for the double-buffer page-flip copy at 1030:3035."""
    mem = cpu.mem
    if getattr(cpu, "pre2_verify_mode", False):
        snap = _spr.snapshot_planes(mem)
        _run_panel(cpu, snap)
        cpu.pre2_frame_panel_pending.append(snap)
        interpret_current_instruction_without_hook(cpu)
        return
    _run_panel(cpu, _spr.plane_views(mem))
    cpu.s.ip = cpu.pop()  # near ret; regs preserved (vsync wait omitted, timing-only)


# NOTE on 1030:3B40 (the frame compositor): it is a static composition —
# sti; [0x2DF0]=1; [0x2DDC]=0x55AA; call 3582; call 3A08; call 3035; pop es; pop ds;
# ret — i.e. draw_grid -> scroll_copy -> panel_copy over the now-native leaves. We do
# NOT wire it as a native replacement: no available demo reaches 3B40 (its leaves are
# exercised via their other callers: 0237 / 01E2 / 023A), so a native 3B40 cannot be
# verified yet. The hybrid runtime already runs the three leaves natively when the ASM
# 3B40 calls them; wire a native compositor only once a scenario exercises 3B40 so it
# can be lockstep-verified (the call order itself is static: 3B4C/3B4F/3B52).


def _planes_match(mem, snap) -> str | None:
    live = _spr.snapshot_planes(mem)
    for p in range(4):
        if bytes(live[p]) != bytes(snap[p]):
            return f"plane {p}"
    return None


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Install the lockstep verify-exit hooks at each routine's RET."""

    def _row_verify(c) -> None:
        if c.pre2_frame_pending:
            entry_di, snap, flags = c.pre2_frame_pending.pop()
            mem = c.mem
            reason = _planes_match(mem, snap)
            if reason is None and (c.s.di & 0xFFFF) != entry_di:
                reason = f"di not preserved {c.s.di & 0xFFFF:04X}!={entry_di:04X}"
            if reason is None:
                if _frame.read_row_flags(mem) != (flags.plane_attr & 0xFF, flags.tile_flags & 0xFF,
                                                  flags.tile_type & 0xFF):
                    reason = "row flags [0x6BB9]/[0x2DEE]/[0x2DF0]"
            report(stats, on_result, raise_on_divergence, "frame_tile_row", reason)
        interpret_current_instruction_without_hook(c)

    def _grid_verify(c) -> None:
        if c.pre2_frame_grid_pending:
            _entry_cam, snap, res = c.pre2_frame_grid_pending.pop()
            mem = c.mem
            reason = _planes_match(mem, snap) if res.redrew else None
            cam = _frame.read_camera(mem)
            pa, tf, tt = _frame.read_row_flags(mem)
            if reason is None and (cam.prev_x, cam.prev_y) != (res.prev_x & 0xFFFF, res.prev_y & 0xFFFF):
                reason = "prev camera [0x2DDC]/[0x2DDE]"
            if reason is None and res.redrew:
                st = _frame.read_scroll_state(mem)
                if tf != (res.tile_flags & 0xFF) or tt != (res.dirty & 0xFF) or st.dirty_rows != (res.dirty_rows & 0xFF):
                    reason = "dirty flags [0x2DEE]/[0x2DF0]/[0x2DF1]"
            report(stats, on_result, raise_on_divergence, "frame_grid", reason)
        interpret_current_instruction_without_hook(c)

    def _scroll_verify(c) -> None:
        if c.pre2_frame_scroll_pending:
            snap = c.pre2_frame_scroll_pending.pop()
            report(stats, on_result, raise_on_divergence, "frame_scroll_copy", _planes_match(c.mem, snap))
        interpret_current_instruction_without_hook(c)

    def _panel_verify(c) -> None:
        if c.pre2_frame_panel_pending:
            snap = c.pre2_frame_panel_pending.pop()
            report(stats, on_result, raise_on_divergence, "frame_panel_copy", _planes_match(c.mem, snap))
        interpret_current_instruction_without_hook(c)

    for exit_addr, fn, name in (
        (_ROW_EXIT, _row_verify, "frame_tile_row_verify"),
        (_GRID_EXIT, _grid_verify, "frame_grid_verify"),
        (_SCROLL_EXIT, _scroll_verify, "frame_scroll_copy_verify"),
        (_PANEL_EXIT, _panel_verify, "frame_panel_copy_verify"),
    ):
        cpu.replacement_hooks[exit_addr] = fn
        cpu.hook_names[exit_addr] = name
