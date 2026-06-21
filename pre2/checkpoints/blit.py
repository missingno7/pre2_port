"""Checkpoint for the per-frame sprite blit (1030:3B88).

Recovered logic: ``pre2.recovered.renderer``; data model: ``pre2.bridge.sprites``.
Merge target: the renderer.

Renders one 16x16 sprite/tile from the planar VRAM cache, dispatching on the type
produced by the (still-ASM) classifier ``1030:4232`` — the recovered blit only
consumes that type table + masks. The original saves/restores its own EGA state (451F/452F), so
the native path leaves the sequencer/GC alone; the only register the caller reads
back is di (advanced by 2 to the next column).
"""

from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from pre2.bridge import sprites as _spr
from pre2.recovered.renderer import blit_sprite, dest_rows

from .common import Pre2HybridGap, report

# GOG build: data seg 1A0F, video code region old+0x1F, ds offsets old+4.
_DATA_SEG = 0x1A0F
_BLIT_ENTRY = (0x1030, 0x3B88)
# the three dispatch RET sites: plain (type 0), empty (type 1), masked (type >=2).
_BLIT_EXITS = ((0x1030, 0x3BF5), (0x1030, 0x3C05), (0x1030, 0x3D83))
_TYPE_TABLE = 0x4DF8       # [0x4DF8+idx] sprite type
_MASK_BASE = 0x2DF8        # [0x2DF8+(id-2)*0x20] transparency mask for partial sprites
_VAR_BG_PTR = 0x2DF6       # [0x2DF6] background source pointer
_VAR_BG_ROW = 0x6BC4       # [0x6BC4] scroll row (bg_off = [0x2DF6] - 0x28*[0x6BC4])


def _blit_inputs(mem, cpu):
    idx = cpu.s.ax & 0xFF
    typ = mem.data[(_DATA_SEG << 4) + _TYPE_TABLE + idx]
    di = cpu.s.di & 0xFFFF
    bg_off = (mem.rw(_DATA_SEG, _VAR_BG_PTR) - 0x28 * mem.data[(_DATA_SEG << 4) + _VAR_BG_ROW]) & 0xFFFF
    mask = b""
    if typ >= 2:
        base = (_DATA_SEG << 4) + _MASK_BASE + (typ - 2) * 0x20
        mask = bytes(mem.data[base: base + 0x20])
    return idx, typ, di, bg_off, mask


def _blit_slot(planes, di):
    return [bytes(planes[p][(d + c) & 0xFFFF] for _r, d in dest_rows(di) for c in range(2))
            for p in range(4)]


@registry.replace(*_BLIT_ENTRY, "sprite_blit")
def sprite_blit(cpu) -> None:
    """Native replacement for the per-sprite blit dispatcher at 1030:3B88."""
    mem = cpu.mem
    if (cpu.s.es & 0xFFFF) != 0xA000:
        raise Pre2HybridGap(
            f"sprite blit with es={cpu.s.es & 0xFFFF:04X} (not A000) at 1030:3B88 "
            "is not recovered — the renderer only targets the A000 planar planes."
        )
    idx, typ, di, bg_off, mask = _blit_inputs(mem, cpu)

    if getattr(cpu, "pre2_verify_mode", False):
        snap = _spr.snapshot_planes(mem)
        blit_sprite(snap, idx, di, typ, bg_off, mask)
        cpu.pre2_blit_pending.append((typ, di, _blit_slot(snap, di), (di + 2) & 0xFFFF))
        interpret_current_instruction_without_hook(cpu)
        return

    blit_sprite(_spr.plane_views(mem), idx, di, typ, bg_off, mask)
    cpu.s.di = (di + 2) & 0xFFFF  # [asm: di advanced one tile column]
    cpu.s.ip = cpu.pop()


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Install the lockstep verify-exit hook at the blit's three RET sites."""

    def _blit_verify_exit(c) -> None:
        # Reached one of the blit's RET sites (verify mode let the ASM draw).
        if c.pre2_blit_pending:
            typ, di, native, exp_di = c.pre2_blit_pending.pop(0)
            asm = _blit_slot(_spr.plane_views(c.mem), di)
            if asm != native:
                reason = "framebuffer"
            elif (c.s.di & 0xFFFF) != exp_di:
                reason = f"exit di {c.s.di & 0xFFFF:04X}!={exp_di:04X}"
            else:
                reason = None
            report(stats, on_result, raise_on_divergence, f"sprite_blit_type{typ}", reason)
        interpret_current_instruction_without_hook(c)

    for exit_addr in _BLIT_EXITS:
        cpu.replacement_hooks[exit_addr] = _blit_verify_exit
        cpu.hook_names[exit_addr] = "sprite_blit_verify"
