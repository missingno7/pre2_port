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

    __slots__ = ("data", "sfx_queue", "particle_capture")

    def __init__(self, data: bytearray):
        if not isinstance(data, bytearray):
            data = bytearray(data)
        if len(data) < ADDR_SPACE:
            data = data + bytearray(ADDR_SPACE - len(data))
        self.data = data
        #: play_sfx TRIGGERS this frame (a list of effect indices). native_play_sfx appends one per CALL — so a
        #: repeated identical effect (e.g. a held attack hitting each frame) fires each time, unlike the single
        #: [0x1004] descriptor which is last-wins. NativeAudio drains it once per displayed frame; capped so a
        #: consumer-less run (the forward oracle) can't grow it without bound.
        self.sfx_queue: list[int] = []
        #: the [0x7DE6] point particles (spider-threads/sparkles/fireflies) snapshotted at the 4B8E ENTRY — i.e.
        #: BEFORE native_particle_consume kills them (they're one-shot). native_render draws from this so the
        #: effects show; None when unset (native_render then reads the live [0x7DE6], empty after the consume).
        self.particle_capture = None

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
