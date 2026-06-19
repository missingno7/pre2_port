"""Generic LZEXE bootstrap helpers for DOS real-mode programs.

The source-port target should be the unpacked game logic, not the transient
packer stub.  This accelerator keeps packed originals bootable while making the
unpacker an explicit, target-neutral bootstrap concern.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Callable

from .cpu import AF, CPU8086

# Entry at LZEXE 0.91 stub offset 0069 after the relocation prelude prepared:
#   DS:SI = compressed stream, ES:DI = output stream, BP = bit buffer, DX = bits left.
SIG_LZEXE_MAIN_LOOP_0069 = bytes.fromhex(
    "d1 ed 4a 75 05 ad 89 c5 b2 10 73 03 a4 eb f1 31 c9"
)


def code_matches(cpu: CPU8086, off: int, expected: bytes) -> bool:
    cs = cpu.s.cs & 0xFFFF
    return all(cpu.mem.rb(cs, (off + i) & 0xFFFF) == b for i, b in enumerate(expected))


def interpret_current_instruction_without_hook(cpu: CPU8086) -> None:
    """Interpret the live instruction when a speculative bootstrap hook moved away.

    Bootstrap unpackers often copy themselves to a new segment.  We may install
    broad offset-based hooks for speed, but each handler must prove that the live
    bytes are still the LZEXE loop before replacing it.
    """
    key = cpu.addr()
    fn = cpu.replacement_hooks.pop(key, None)
    name = cpu.hook_names.pop(key, None)
    try:
        cpu.step()
    finally:
        if fn is not None:
            cpu.replacement_hooks[key] = fn
        if name is not None:
            cpu.hook_names[key] = name


def _lodsb(cpu: CPU8086, ds: int, si: int) -> tuple[int, int]:
    value = cpu.mem.rb(ds, si)
    return value, (si + 1) & 0xFFFF


def _lodsw(cpu: CPU8086, ds: int, si: int) -> tuple[int, int]:
    value = cpu.mem.rw(ds, si)
    return value, (si + 2) & 0xFFFF


def _next_bit(cpu: CPU8086, *, ds: int, si: int, bp: int, dx: int, af: bool) -> tuple[int, int, int, int, bool]:
    bit = bp & 0x0001
    bp = (bp >> 1) & 0xFFFF
    af = (dx & 0x000F) == 0
    dx = (dx - 1) & 0xFFFF
    if dx == 0:
        bp, si = _lodsw(cpu, ds, si)
        dx = (dx & 0xFF00) | 0x10
    return bit, si, bp, dx, af


def run_lzexe_bootstrap_main_loop_0069(cpu: CPU8086, *, max_ops: int = 8_000_000) -> None:
    """Run the hot LZEXE 0.91 bitstream loop and resume at stub offset 00FCh."""
    s = cpu.s
    mem = cpu.mem
    ds = s.ds & 0xFFFF
    es = s.es & 0xFFFF
    si = s.si & 0xFFFF
    di = s.di & 0xFFFF
    bp = s.bp & 0xFFFF
    dx = s.dx & 0xFFFF
    bx = s.bx & 0xFFFF
    cx = s.cx & 0xFFFF
    al = s.ax & 0x00FF
    af = bool(s.flags & AF)

    ops = 0
    while True:
        ops += 1
        if ops > max_ops:
            raise RuntimeError(
                f"LZEXE bootstrap loop at {s.cs:04X}:0069 did not finish within {max_ops} operations"
            )

        bit, si, bp, dx, af = _next_bit(cpu, ds=ds, si=si, bp=bp, dx=dx, af=af)
        if bit:
            al = mem.rb(ds, si)
            mem.wb(es, di, al)
            si = (si + 1) & 0xFFFF
            di = (di + 1) & 0xFFFF
            continue

        cx = 0
        bit, si, bp, dx, af = _next_bit(cpu, ds=ds, si=si, bp=bp, dx=dx, af=af)
        if bit:
            ax, si = _lodsw(cpu, ds, si)
            bx = ax & 0xFFFF
            bh = (bx >> 8) & 0xFF
            ah = (ax >> 8) & 0xFF
            bh = ((bh >> 3) | 0xE0) & 0xFF
            bx = ((bh << 8) | (bx & 0x00FF)) & 0xFFFF
            ah &= 0x07
            if ah:
                cx = (ah + 2) & 0xFFFF
            else:
                al, si = _lodsb(cpu, ds, si)
                if al == 0:
                    s.ds = ds
                    s.es = es
                    s.si = si
                    s.di = di
                    s.bp = bp
                    s.dx = dx
                    s.bx = bx
                    s.cx = 0x0003
                    s.ax = 0x0000
                    cpu.set_logic_flags(0, 8)
                    if af:
                        s.flags |= AF
                    else:
                        s.flags &= ~AF
                    s.ip = 0x00FC
                    return
                if al == 1:
                    bx = di
                    di = ((di & 0x000F) + 0x2000) & 0xFFFF
                    es = (es + (bx >> 4) - 0x0200) & 0xFFFF
                    bx = si
                    si &= 0x000F
                    ds = (ds + (bx >> 4)) & 0xFFFF
                    continue
                cx = (al + 1) & 0xFFFF
        else:
            bit, si, bp, dx, af = _next_bit(cpu, ds=ds, si=si, bp=bp, dx=dx, af=af)
            cx = ((cx << 1) | bit) & 0xFFFF
            bit, si, bp, dx, af = _next_bit(cpu, ds=ds, si=si, bp=bp, dx=dx, af=af)
            cx = ((cx << 1) | bit) & 0xFFFF
            cx = (cx + 2) & 0xFFFF
            al, si = _lodsb(cpu, ds, si)
            bx = (0xFF00 | al) & 0xFFFF

        for _ in range(cx):
            al = mem.rb(es, (bx + di) & 0xFFFF)
            mem.wb(es, di, al)
            di = (di + 1) & 0xFFFF
        cx = 0


def make_lzexe_0069_hook(name: str = "lzexe_bootstrap_main_loop_0069") -> Callable[[CPU8086], None]:
    def hook(cpu: CPU8086) -> None:
        if not code_matches(cpu, 0x0069, SIG_LZEXE_MAIN_LOOP_0069):
            interpret_current_instruction_without_hook(cpu)
            return
        run_lzexe_bootstrap_main_loop_0069(cpu)

    hook.__name__ = name
    return hook


def install_lzexe_0069_accelerator(
    cpu: CPU8086,
    *,
    segments: Iterable[int] | None = None,
    start_segment: int = 0x1000,
    end_segment: int = 0xA000,
    name_prefix: str = "lzexe",
) -> int:
    """Install guarded LZEXE loop hooks for likely real-mode unpacker segments.

    The hook is intentionally broad but self-checking: if CS:0069 is not the
    known LZEXE loop, it temporarily removes itself and executes the original
    instruction.  This gives packed games a fast cold start without hardcoding a
    game's temporary relocation segment.
    """
    hook = make_lzexe_0069_hook(f"{name_prefix}_bootstrap_lzexe_main_loop_0069")
    count = 0
    if segments is None:
        segments = range(start_segment & 0xFFFF, end_segment & 0xFFFF)
    for seg in segments:
        key = (seg & 0xFFFF, 0x0069)
        if key in cpu.replacement_hooks:
            continue
        cpu.replacement_hooks[key] = hook
        cpu.hook_names[key] = hook.__name__
        count += 1
    return count
