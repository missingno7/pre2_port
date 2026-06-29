"""``NativeGameState`` — the recovered game's memory, owned without a VM.

It *is* the game's address space (a 1 MB ``bytearray`` exposed as ``.data``, exactly what the emulated VM's
``mem`` exposes), so every recovered function and every ``pre2/bridge`` adapter that already reads/writes VM
memory runs over it unchanged. That is the migration's adapter swap: the recovered function is the shared
centre; today a VM ``mem`` is one adapter, a ``NativeGameState`` is another — one implementation, two adapters,
no second copy that can drift.

Seeded today from a snapshot/VM (the bootstrap). As islands move to source-level state ownership, more of the
per-frame update reads/writes this image natively and the VM is needed only as the verify oracle.
"""
from __future__ import annotations

ADDR_SPACE = 0x100000   # 1 MB real-mode address space (DGROUP @ DS<<4, tilemap @ [0x2DDA], framebuffer, ...)
DATA_SEG = 0x1A0F       # the game data segment (DGROUP)


class NativeGameState:
    """The recovered game's memory image. Exposes ``.data`` (the 1 MB address space) so the existing bridges
    — which take a ``mem``-like object and index ``mem.data`` — read and write it with no change."""

    __slots__ = ("data",)

    def __init__(self, data: bytearray):
        if not isinstance(data, bytearray):
            data = bytearray(data)
        if len(data) < ADDR_SPACE:
            data = data + bytearray(ADDR_SPACE - len(data))
        self.data = data

    @classmethod
    def from_vm(cls, rt) -> "NativeGameState":
        """Seed from a loaded VM runtime (a snapshot's memory) — the bootstrap into native state ownership."""
        return cls(bytearray(rt.cpu.mem.data))

    def rb(self, off: int) -> int:
        """Read a DGROUP byte (DS-relative), the recovered functions' ``rb`` accessor."""
        return self.data[((DATA_SEG << 4) + (off & 0xFFFF)) & 0xFFFFF]

    def rw(self, off: int) -> int:
        """Read a DGROUP word (DS-relative), the recovered functions' ``rw`` accessor."""
        b = ((DATA_SEG << 4) + (off & 0xFFFF)) & 0xFFFFF
        return self.data[b] | (self.data[(b + 1) & 0xFFFFF] << 8)
