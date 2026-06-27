"""Checkpoint for the player ground/tile collision routine (1030:5A96..5B80).

This is the largest remaining ASM piece of the per-frame player update — a real CALL/RET subroutine invoked at
``5A41`` (right after the Y integrate). It resolves the player against the tile map: the tile-cell calc, the
camera-range trigger, the tile-interaction worker (bridge-dip + ground dispatch + ceiling), the post-worker
fall/land, and the horizontal body scan. All recovered in :func:`pre2.recovered.player_collision.collision`,
proven byte-exact in shadow (3515 calls, 0 mismatches, 0 gaps across six demos).

Thin VM contact point: read ``[0x4F1C]``-struct DS state + the tile map (``es=[0x2DDA]``), run ``collision`` to
get the ``(ds_writes, map_writes)`` byte-write contract, then — in the live hybrid — apply it and emulate the
RET. In verify mode the original ASM is the oracle: the hook predicts (no mutation) and the verify-exit hook at
``5B80`` diffs every predicted byte against the ASM's. An unrecovered path fails loud (live) / is reported as a
gap (verify), never a silent fallback.
"""
from __future__ import annotations

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.hooks import registry
from pre2.recovered.player_collision import collision

from .common import Pre2HybridGap, report

_ENTRY = (0x1030, 0x5A96)   # the collision routine entry (call target from 5A41)
_EXIT = (0x1030, 0x5B80)    # ret
_DS = 0x1A0F
_MAP_SEG_PTR = 0x2DDA       # [0x2DDA] holds the tile-map segment (es)


def _context(mem):
    """Build the (rb, rw, read_es) readers + the DS / map base addresses for this call."""
    ds_base = (_DS << 4) & 0xFFFFF

    def rb(o):
        return mem.data[(ds_base + (o & 0xFFFF)) & 0xFFFFF]

    def rw(o):
        b = (ds_base + (o & 0xFFFF)) & 0xFFFFF
        return mem.data[b] | (mem.data[(b + 1) & 0xFFFFF] << 8)

    es_base = ((rw(_MAP_SEG_PTR) << 4)) & 0xFFFFF

    def read_es(o):
        return mem.data[(es_base + (o & 0xFFFF)) & 0xFFFFF]

    return rb, rw, read_es, ds_base, es_base


@registry.replace(*_ENTRY, "player_collision")
def player_collision_hook(cpu) -> None:
    """Native replacement for the player ground/tile collision at 1030:5A96."""
    mem = cpu.mem
    rb, rw, read_es, ds_base, es_base = _context(mem)

    if getattr(cpu, "pre2_verify_mode", False):
        try:
            ds_w, map_w = collision(rb, rw, read_es)
        except NotImplementedError as exc:
            ds_w = map_w = None
            cpu.pre2_collision_gap = str(exc)
        cpu.pre2_collision_pending.append((ds_w, map_w, es_base))
        interpret_current_instruction_without_hook(cpu)
        return

    # Live hybrid: run the recovered routine, apply the write-contract, emulate the RET (pop the return address
    # pushed by `call 0x5A96` at 5A41). An unrecovered path must fail loud, never silently run the ASM.
    try:
        ds_w, map_w = collision(rb, rw, read_es)
    except NotImplementedError as exc:
        raise Pre2HybridGap(f"player collision (5A96): {exc}") from exc
    for a, v in ds_w.items():
        mem.data[(ds_base + (a & 0xFFFF)) & 0xFFFFF] = v & 0xFF
    for o, v in map_w.items():
        mem.data[(es_base + (o & 0xFFFF)) & 0xFFFFF] = v & 0xFF
    cpu.s.ip = cpu.pop()


def register_verify(cpu, stats, on_result, raise_on_divergence) -> None:
    """Install the lockstep verify hook at the routine return (5B80): diff every predicted DS + map byte
    (computed at entry from the pre-state) against the ASM's post-state."""

    def _verify_at_exit(c) -> None:
        pending = getattr(c, "pre2_collision_pending", None)
        if pending:
            ds_w, map_w, es_base = pending.pop()
            if ds_w is None:                                          # an unrecovered path was hit
                report(stats, on_result, raise_on_divergence, "player_collision",
                        f"gap: {getattr(c, 'pre2_collision_gap', '?')}")
            else:
                ds_base = (_DS << 4) & 0xFFFFF
                reason = None
                for a, v in ds_w.items():
                    act = c.mem.data[(ds_base + (a & 0xFFFF)) & 0xFFFFF]
                    if act != (v & 0xFF):
                        reason = f"ds[{a:#06x}] rec={v & 0xFF:#04x} asm={act:#04x}"
                        break
                if reason is None:
                    for o, v in map_w.items():
                        act = c.mem.data[(es_base + (o & 0xFFFF)) & 0xFFFFF]
                        if act != (v & 0xFF):
                            reason = f"map[{o:#06x}] rec={v & 0xFF:#04x} asm={act:#04x}"
                            break
                report(stats, on_result, raise_on_divergence, "player_collision", reason)
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[_EXIT] = _verify_at_exit
    cpu.hook_names[_EXIT] = "player_collision_verify"
