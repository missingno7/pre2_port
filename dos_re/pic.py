"""Minimal 8259 PIC (master) — enough for IRQ0 (timer) and a Sound Blaster IRQ.

Generic PC hardware: devices raise IRQ lines (set the request register), the mask
register gates them, and the CPU acknowledges the highest-priority *unmasked*
request at an instruction boundary, runs its ISR, and the ISR sends EOI.  Crucially
a request raised while masked stays pending and is delivered once unmasked — that
is what lets a Sound Blaster's block-complete IRQ keep driving playback across the
brief windows the driver masks the line.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PIC8259:
    imr: int = 0xFF   # interrupt mask register (1 = masked); BIOS unmasks what it needs
    irr: int = 0x00   # interrupt request register (pending)
    isr: int = 0x00   # in-service register

    def raise_irq(self, n: int) -> None:
        self.irr |= 1 << (n & 7)

    def set_mask(self, value: int) -> None:
        self.imr = value & 0xFF

    def get_mask(self) -> int:
        return self.imr & 0xFF

    def eoi(self) -> None:
        # Non-specific EOI: clear the highest-priority (lowest-numbered) in-service bit.
        if self.isr:
            self.isr &= self.isr - 1

    def acknowledge(self) -> int | None:
        """Return the IRQ number to deliver now (and mark it in service), or None.

        One interrupt in service at a time (no nesting); combined with the CPU
        clearing IF on entry this matches how the simple ISRs here behave.
        """
        if self.isr:
            return None
        pending = self.irr & ~self.imr & 0xFF
        if not pending:
            return None
        cand = pending & -pending          # lowest set bit = highest priority
        self.irr &= ~cand
        self.isr |= cand
        return cand.bit_length() - 1
