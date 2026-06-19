"""Synchronous delivery of hardware interrupts to the interpreted game.

The interpreter has no asynchronous interrupt mechanism, but a DOS game can install
real ISRs (timer on INT 08h, keyboard on INT 09h) and drives input through them.
At a safe instruction boundary a front-end can ask the runtime to *deliver* an
interrupt exactly the way hardware would: push FLAGS/CS/IP, clear IF/TF, jump to
the vector from the IVT, and run the interpreter until the matching ``iret``
returns to the original instruction.

This keeps the game as the oracle -- the game's own ISR runs and updates its own
state (e.g. the keyboard scan-code table) -- instead of guessing that state.
"""
from __future__ import annotations

from .cpu import IF, TF
from .runtime import Runtime


def read_vector(rt: Runtime, num: int) -> tuple[int, int]:
    """Return (segment, offset) of interrupt vector ``num`` from the real IVT."""
    mem = rt.cpu.mem
    off = mem.rw(0, (num * 4) & 0xFFFFF)
    seg = mem.rw(0, (num * 4 + 2) & 0xFFFFF)
    return seg, off


def deliver_interrupt(rt: Runtime, num: int, *, max_steps: int = 200_000) -> bool:
    """Invoke the installed handler for interrupt ``num`` and run it to its iret.

    Returns False (a no-op) if no handler is installed.  Must be called at an
    instruction boundary, i.e. between ``rt.cpu.run(...)`` batches, never from
    inside a step.
    """
    cpu = rt.cpu
    seg, off = read_vector(rt, num)
    if seg == 0 and off == 0:
        return False

    ret_cs, ret_ip = cpu.s.cs & 0xFFFF, cpu.s.ip & 0xFFFF
    sp0 = cpu.s.sp & 0xFFFF
    # Hardware interrupt entry sequence.
    cpu.push(cpu.s.flags)
    cpu.push(ret_cs)
    cpu.push(ret_ip)
    cpu.set_flag(IF, False)
    cpu.set_flag(TF, False)
    cpu.s.cs, cpu.s.ip = seg & 0xFFFF, off & 0xFFFF

    steps = 0
    while not (cpu.s.sp == sp0 and cpu.addr() == (ret_cs, ret_ip)):
        cpu.step()
        steps += 1
        if steps > max_steps:
            raise RuntimeError(f"INT {num:02X}h handler did not return (cs:ip={cpu.s.cs:04X}:{cpu.s.ip:04X})")
    return True


def deliver_scancode(rt: Runtime, scancode: int, *, max_steps: int = 200_000) -> bool:
    """Present a raw keyboard scan code on port 60h and run the INT 9 handler.

    ``scancode`` is an XT make code (e.g. 0x1C Enter, 0x48 Up) for a press, or the
    make code OR 0x80 for a release.  The game's own ISR translates it into its
    key-state table, so no game-side key semantics are reimplemented here.
    """
    rt.dos.current_scancode = scancode & 0xFF
    return deliver_interrupt(rt, 0x09, max_steps=max_steps)
