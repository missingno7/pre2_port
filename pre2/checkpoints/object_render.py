"""Checkpoint for the moving-sprite renderer (1030:26FA — draw the active sprite list).

Thin VM contact point: it reads the active-sprite list + per-sprite attributes +
camera through the bridge (``pre2.bridge.object_render``), runs the recovered planner
+ planar blit straight onto the four EGA shadow planes (in place — no 256 KiB copy),
writes the routine's record-mutation contract back, and near-returns. No renderer
logic lives here.

Live-hooked: in hybrid play the recovered renderer draws every moving sprite (the
hottest gameplay routine). In verify mode the original ASM is the oracle and the
recovered planes are diffed against it at the routine's RET (2DF9).
"""
from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from pre2.bridge import object_render as _obj
from pre2.recovered.object_render import paint_sprite, plan_sprite

from .common import Pre2HybridGap, report

_ENTRY = (0x1030, 0x26FA)
_EXIT = (0x1030, 0x2DF9)
_DATA_SEG = 0x1A0F
_FRAME = 0x6BD5          # [asm 2708: inc word [6bd5]]


def _planes_view(mem):
    """Writable views onto the four EGA shadow planes (no copy)."""
    mv = memoryview(mem.data)
    return [mv[EGA_APERTURE + p * EGA_PLANE_STRIDE: EGA_APERTURE + (p + 1) * EGA_PLANE_STRIDE]
            for p in range(4)]


def _render(mem, planes, *, mutate: bool, frame_pre_inc: bool) -> None:
    cam = _obj.read_camera(mem, frame_pre_inc=frame_pre_inc)
    for off, spr in _obj.read_active_list(mem):
        if spr.sprite_id == 0xFFFF:                      # [asm 2713] empty slot
            continue
        if (spr.sprite_id & 0x5FFF) == 0x135:            # [asm 277E] fixed-screen HUD sprite
            raise Pre2HybridGap("special HUD sprite id 0x135 (1030:2784, no-camera path) is not recovered")
        flat5 = ((_DATA_SEG << 4) + off + 5) & 0xFFFFF
        if mutate:                                       # [asm 2732] clear drawn bit; [2742] dec life
            mem.data[flat5] &= 0xDF
            fl = ((_DATA_SEG << 4) + off + 0x11) & 0xFFFFF
            life = mem.data[fl]
            mem.data[fl] = (life - 1) & 0xFF if life else 0
        draw = plan_sprite(spr, _obj.read_attr(mem, spr.sprite_id), cam)
        if draw is None:
            continue
        if mutate:                                       # [asm 28B6 or 0x20 / 28BA and 0xBF] mark drawn
            mem.data[flat5] = (mem.data[flat5] | 0x20) & 0xBF
        src = _obj.read_source(mem, draw.src_seg, draw.src_off, draw.src_bw * draw.full_rows * 6 + 64)
        paint_sprite(planes, draw, src, cam.row_stride)


@registry.replace(*_ENTRY, "object_render")
def object_render(cpu) -> None:
    """Native replacement for the moving-sprite renderer at 1030:26FA."""
    mem = cpu.mem

    if getattr(cpu, "pre2_verify_mode", False):
        # The ASM oracle runs (interpret below) and increments the frame counter [6bd5]
        # itself at 2708, so we must NOT pre-increment it here — doing both double-counts
        # and shifts the blink phase (frame & 3) by one, which silently corrupts every
        # *blinking* sprite (hit-flash / invincibility) in verify mode only. Instead read
        # the value the ASM will use (orig + 1) logically, without touching memory. Paint
        # onto a copy (no record mutation); diff at the RET.
        snap = [bytearray(p) for p in _planes_view(mem)]
        _render(mem, snap, mutate=False, frame_pre_inc=True)
        cpu.pre2_object_pending.append(snap)
        interpret_current_instruction_without_hook(cpu)
        return

    # Hybrid: the ASM does NOT run, so apply its [6bd5] increment ourselves [asm 2708].
    fl = ((_DATA_SEG << 4) + _FRAME) & 0xFFFFF
    v = ((mem.data[fl] | (mem.data[fl + 1] << 8)) + 1) & 0xFFFF
    mem.data[fl] = v & 0xFF
    mem.data[fl + 1] = (v >> 8) & 0xFF
    _render(mem, _planes_view(mem), mutate=True, frame_pre_inc=False)
    cpu.s.ip = cpu.pop()  # near ret (caller's regs are preserved across the routine)


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Install the lockstep verify-exit hook at 26FA's RET (2DF9)."""

    def _verify_at_exit(c) -> None:
        if c.pre2_object_pending:
            rec = c.pre2_object_pending.pop()
            asm = _planes_view(c.mem)
            reason = None
            for p in range(4):
                if rec[p] != asm[p]:
                    i = next(k for k in range(len(rec[p])) if rec[p][k] != asm[p][k])
                    reason = f"plane{p} @{i:04X}: asm={asm[p][i]:02X} rec={rec[p][i]:02X}"
                    break
            report(stats, on_result, raise_on_divergence, "object_render", reason)
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[_EXIT] = _verify_at_exit
    cpu.hook_names[_EXIT] = "object_render_verify"
