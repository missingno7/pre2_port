"""Checkpoint for the scene background scroll-blit block (1030:965A..969C).

The mode-select / map scroll generates its background by blitting one fresh byte-column of
the master pattern (segment ``[0x2875]``) into the EGA planes every 8 px of pan — a hot
``movsb`` loop (200 rows x 4 planes) that, run as interpreted ASM, dominates the scroll
frame (~half its cost). Replacing it natively lets the VM keep up with the present rate so
the scroll renders smoothly. It is **vsync-gated**, so this does not change the scroll
*speed* — only the per-frame CPU cost.

Thin VM contact point: read scroll_x + the master segment via ``pre2.bridge.present``, run
the recovered ``scroll_blit_column`` onto the planes, advance the scroll counter
(``[0xB19D]``), restore ``ds`` to DGROUP, and continue at 969C (an inline fall-through block
— only ``ip`` advances, no stack change). The controller after 969C reads only ``[0x27E8]``
/``[0xB19D]`` (both written), never the block's scratch registers.

Live-hooked: the recovered blit writes the planes natively. In verify mode the original ASM
is the oracle and the recovered planes are diffed at the block exit (969C). Verified
byte-exact over 79 blits / 553 skips (pre2/probes/verify_scroll_blit.py).
"""
from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from pre2.bridge import present as _pr
from pre2.recovered.present import scroll_blit_column, scroll_shift_frame

from .common import report

_ENTRY = (0x1030, 0x965A)
_EXIT = (0x1030, 0x969C)
_SHIFT_ENTRY = (0x1030, 0x9804)
_SHIFT_EXIT = (0x1030, 0x9877)
_DGROUP = 0x1A0F


def _plane_diff(rec, got):
    """First-differing (plane, offset) of two plane-buffer lists, or None if identical."""
    for p in range(4):
        if rec[p] != got[p]:
            i = next(k for k in range(len(got[p])) if got[p][k] != rec[p][k])
            return f"plane{p} @ {i:#06x}: asm={got[p][i]:#04x} rec={rec[p][i]:#04x}"
    return None


@registry.replace(*_ENTRY, "scroll_blit")
def scroll_blit(cpu) -> None:
    """Native replacement for the background scroll-blit block at 1030:965A."""
    mem, s = cpu.mem, cpu.s
    sx, source = _pr.read_scroll_inputs(mem)

    if getattr(cpu, "pre2_verify_mode", False):
        rec = _pr.read_planes(mem)
        scroll_blit_column(rec, source, sx)
        cpu.pre2_scroll_pending.append(rec)
        interpret_current_instruction_without_hook(cpu)
        return

    scroll_blit_column(_pr.plane_views(mem), source, sx)   # write VRAM in place
    _pr.advance_scroll_x(mem, sx)                           # [asm 965E: inc [0xB19D]]
    s.ds = _DGROUP                                          # [asm 9697: ds reloaded to 0x1A0F]
    s.ip = _EXIT[1]                                         # fall-through block: advance ip, no stack change


@registry.replace(*_SHIFT_ENTRY, "scroll_shift")
def scroll_shift(cpu) -> None:
    """Native replacement for the menu/scene framebuffer scroll block at 1030:9804 (the
    mode-select's hottest op — a 4-plane A000 self-copy following the camera). The wrap mask
    is the live ``bp`` register. Inline fall-through block: advance ``ip`` to 9877, restore
    ``ds`` to DGROUP; no stack change."""
    mem, s = cpu.mem, cpu.s
    b199, sx, sy, psy, pd = _pr.read_scroll_shift_inputs(mem)

    if getattr(cpu, "pre2_verify_mode", False):
        rec = _pr.read_planes(mem)
        scroll_shift_frame(rec, b199, sx, sy, psy, pd, wrap=s.bp)
        cpu.pre2_scroll_shift_pending.append(rec)
        interpret_current_instruction_without_hook(cpu)
        return

    scroll_shift_frame(_pr.plane_views(mem), b199, sx, sy, psy, pd, wrap=s.bp)
    s.ds = _DGROUP                                          # [asm 9831/9872: ds reloaded to 0x1A0F]
    s.ip = _SHIFT_EXIT[1]


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Install the lockstep verify hooks at each block's exit: diff the recovered planes
    (predicted at entry from the before-planes) against the ASM's planes."""

    def _scroll_blit_exit(c) -> None:
        if getattr(c, "pre2_scroll_pending", None):
            report(stats, on_result, raise_on_divergence, "scroll_blit",
                   _plane_diff(c.pre2_scroll_pending.pop(), _pr.read_planes(c.mem)))
        interpret_current_instruction_without_hook(c)

    def _scroll_shift_exit(c) -> None:
        if getattr(c, "pre2_scroll_shift_pending", None):
            report(stats, on_result, raise_on_divergence, "scroll_shift",
                   _plane_diff(c.pre2_scroll_shift_pending.pop(), _pr.read_planes(c.mem)))
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[_EXIT] = _scroll_blit_exit
    cpu.hook_names[_EXIT] = "scroll_blit_verify"
    cpu.replacement_hooks[_SHIFT_EXIT] = _scroll_shift_exit
    cpu.hook_names[_SHIFT_EXIT] = "scroll_shift_verify"
