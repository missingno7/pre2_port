from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .mz import MZExecutable, parse_mz


CPU_MEM_SIZE = 1024 * 1024
PSP_SIZE = 256
DEFAULT_LOAD_SEGMENT = 0x1000


def linear(seg: int, off: int) -> int:
    return (((seg & 0xFFFF) << 4) + (off & 0xFFFF)) & 0xFFFFF


EGA_CPU_APERTURE = 0xA0000    # CPU-visible base of the real EGA A000h aperture
# Store emulated EGA bitplanes outside the 20-bit CPU address space.  Earlier
# revisions stored plane 1/2/3 at A000:2000/4000/6000, but those are real CPU
# offsets/pages, not hardware planes.  Full-screen transition code can legally
# write there, which corrupted the displayed plane shadows and mixed screens.
EGA_APERTURE = 0x100000       # compatibility name: shadow-plane storage base
EGA_PLANE_STRIDE = 0x10000    # full 64 KiB per EGA plane
EGA_PLANE_WINDOW = 0x10000    # CPU offsets 0000h..FFFFh map into each plane
EGA_VISIBLE_PLANE_SIZE = 0x2000  # 320x200x4bpp visible bytes per plane
EGA_SHADOW_SIZE = EGA_PLANE_STRIDE * 4
MEM_SIZE = CPU_MEM_SIZE + EGA_SHADOW_SIZE


class Memory:
    def __init__(self, size: int = MEM_SIZE):
        self.data = bytearray(size)
        self.size = size
        # EGA planar emulation.  Real EGA exposes four hardware bitplanes behind
        # the same CPU offsets in A000h; the sequencer map-mask register
        # (03C4h index 02h) selects which planes a write lands in.  Keep those
        # emulated planes in non-CPU-visible storage at EGA_APERTURE so real CPU
        # offsets like A000:2000 remain usable as offsets/pages instead of
        # colliding with the display shadow for plane 1.
        self.ega_planar = False
        self.ega_map_mask = 0x0F
        # EGA graphics-controller read map select (GC index 04h).  Real EGA
        # reads one selected plane through the same A000h CPU offset.  Hooks and
        # the interpreter both go through rb/rw for normal memory reads, so
        # tracking this keeps plane-to-linear copies from accidentally reading
        # plane 0 four times.
        self.ega_read_plane = 0
        # Minimal VGA/EGA write-mode state.  PRE2 probes 100% VGA
        # compatibility by relying on GC data-rotate/logical-op behaviour:
        # after reading a byte into the VGA latches, a rotated write can OR with
        # that latch.  Keep the model narrow but hardware-shaped so later source
        # port work does not bake in a fake bypass.
        self.ega_data_rotate = 0
        self.ega_logical_op = 0
        self.ega_latches = [0, 0, 0, 0]
        # CRTC start address programmed through 03D4h/03D5h indexes 0Ch/0Dh.
        # Some EGA games use off-screen A000 pages during transitions; the
        # live renderer must display the hardware-selected start offset, not
        # always shadow offset 0000h.
        self.ega_display_start = 0
        # Optional write-watch callbacks used by runtime-code patch tracing.
        # The hot path only pays for one empty-list check per write.  Callbacks
        # receive (physical_20bit_addr, old_bytes, new_bytes).
        self.write_watchers = []

    def check(self, addr: int, n: int = 1) -> int:
        addr &= 0xFFFFF
        if addr + n > self.size:
            raise MemoryError(f"memory access past 1MB: {addr:05X}+{n}")
        return addr

    def rb_phys(self, addr: int) -> int:
        return self.data[self.check(addr)]

    def rw_phys(self, addr: int) -> int:
        addr = self.check(addr, 2)
        return self.data[addr] | (self.data[addr + 1] << 8)

    def _notify_write(self, addr: int, old: bytes, new: bytes) -> None:
        if self.write_watchers:
            for watcher in tuple(self.write_watchers):
                watcher(addr & 0xFFFFF, old, new)

    def wb_phys(self, addr: int, value: int) -> None:
        addr = self.check(addr)
        new = bytes([value & 0xFF])
        if self.write_watchers:
            old = bytes([self.data[addr]])
            self.data[addr] = new[0]
            self._notify_write(addr, old, new)
        else:
            self.data[addr] = new[0]

    def ww_phys(self, addr: int, value: int) -> None:
        addr = self.check(addr, 2)
        new = bytes([value & 0xFF, (value >> 8) & 0xFF])
        if self.write_watchers:
            old = bytes(self.data[addr:addr + 2])
            self.data[addr] = new[0]
            self.data[addr + 1] = new[1]
            self._notify_write(addr, old, new)
        else:
            self.data[addr] = new[0]
            self.data[addr + 1] = new[1]

    # Hot path: inline the 20-bit address calculation and skip the linear()/
    # check()/*_phys() call chain.  ``addr`` is always masked to 0..0xFFFFF, so a
    # byte access is always in range; word accesses wrap at the 1 MB boundary like
    # real-mode hardware instead of raising.
    def rb(self, seg: int, off: int) -> int:
        a = ((((seg & 0xFFFF) << 4) + (off & 0xFFFF)) & 0xFFFFF)
        if self.ega_planar:
            po = a - EGA_CPU_APERTURE
            if 0 <= po < EGA_PLANE_WINDOW:
                base = EGA_APERTURE + po
                self.ega_latches = [
                    self.data[base],
                    self.data[base + EGA_PLANE_STRIDE],
                    self.data[base + EGA_PLANE_STRIDE * 2],
                    self.data[base + EGA_PLANE_STRIDE * 3],
                ]
                return self.ega_latches[self.ega_read_plane & 0x03]
        return self.data[a]

    def rw(self, seg: int, off: int) -> int:
        a = (((seg & 0xFFFF) << 4) + (off & 0xFFFF)) & 0xFFFFF
        d = self.data
        if self.ega_planar:
            po = a - EGA_CPU_APERTURE
            if 0 <= po < EGA_PLANE_WINDOW:
                lo = self.rb(seg, off)
                if po + 1 < EGA_PLANE_WINDOW:
                    hi = self.rb(seg, (off + 1) & 0xFFFF)
                    return lo | (hi << 8)
                return lo | (d[(a + 1) & 0xFFFFF] << 8)
        if a == 0xFFFFF:
            return d[a] | (d[0] << 8)
        return d[a] | (d[a + 1] << 8)

    def wb(self, seg: int, off: int, value: int) -> None:
        a = ((((seg & 0xFFFF) << 4) + (off & 0xFFFF)) & 0xFFFFF)
        if self.ega_planar:
            po = a - EGA_CPU_APERTURE
            if 0 <= po < EGA_PLANE_WINDOW:
                self._ega_wb(po, value)
                return
        v = value & 0xFF
        if self.write_watchers:
            old = bytes([self.data[a]])
            self.data[a] = v
            self._notify_write(a, old, bytes([v]))
        else:
            self.data[a] = v

    def ww(self, seg: int, off: int, value: int) -> None:
        a = (((seg & 0xFFFF) << 4) + (off & 0xFFFF)) & 0xFFFFF
        d = self.data
        if self.ega_planar:
            po = a - EGA_CPU_APERTURE
            if 0 <= po < EGA_PLANE_WINDOW:
                self._ega_wb(po, value & 0xFF)
                if po + 1 < EGA_PLANE_WINDOW:
                    self._ega_wb(po + 1, (value >> 8) & 0xFF)
                else:
                    d[a + 1] = (value >> 8) & 0xFF
                return
        lo = value & 0xFF
        hi = (value >> 8) & 0xFF
        if self.write_watchers:
            if a == 0xFFFFF:
                old0 = bytes([d[a]])
                old1 = bytes([d[0]])
                d[a] = lo
                d[0] = hi
                self._notify_write(a, old0, bytes([lo]))
                self._notify_write(0, old1, bytes([hi]))
            else:
                old = bytes(d[a:a + 2])
                d[a] = lo
                d[a + 1] = hi
                self._notify_write(a, old, bytes([lo, hi]))
        else:
            d[a] = lo
            if a == 0xFFFFF:
                d[0] = hi
            else:
                d[a + 1] = hi

    def _ega_wb(self, plane_off: int, value: int) -> None:
        """Route one A000h byte into the shadow planes the map mask selects."""
        v = value & 0xFF
        rot = self.ega_data_rotate & 0x07
        if rot:
            v = ((v >> rot) | ((v << (8 - rot)) & 0xFF)) & 0xFF
        m = self.ega_map_mask
        d = self.data
        base = EGA_APERTURE + plane_off
        op = self.ega_logical_op & 0x03
        for plane in range(4):
            if not (m & (1 << plane)):
                continue
            addr = base + EGA_PLANE_STRIDE * plane
            latch = self.ega_latches[plane] & 0xFF
            if op == 0:
                out = v
            elif op == 1:
                out = v & latch
            elif op == 2:
                out = v | latch
            else:
                out = v ^ latch
            d[addr] = out & 0xFF

    def load(self, seg: int, off: int, payload: bytes) -> None:
        addr = self.check(linear(seg, off), len(payload))
        if self.write_watchers:
            old = bytes(self.data[addr:addr + len(payload)])
            new = bytes(payload)
            self.data[addr:addr + len(payload)] = new
            self._notify_write(addr, old, new)
        else:
            self.data[addr:addr + len(payload)] = payload

    def block(self, seg: int, off: int, n: int) -> bytes:
        addr = self.check(linear(seg, off), n)
        return bytes(self.data[addr:addr+n])


@dataclass
class LoadedProgram:
    exe: MZExecutable
    memory: Memory
    psp_segment: int
    load_segment: int
    entry_cs: int
    entry_ip: int
    initial_ss: int
    initial_sp: int
    overlay: bytes


def create_psp(memory: Memory, psp_segment: int, command_tail: bytes = b"") -> None:
    # Minimal PSP. Enough for DOS startup code that expects INT 20h and command tail.
    memory.wb(psp_segment, 0x00, 0xCD)
    memory.wb(psp_segment, 0x01, 0x20)
    memory.ww(psp_segment, 0x02, 0x9FFF)
    memory.wb(psp_segment, 0x80, min(len(command_tail), 126))
    memory.load(psp_segment, 0x81, command_tail[:126] + b"\r")


def load_mz_program(path: str | Path, *, psp_segment: int = DEFAULT_LOAD_SEGMENT,
                    command_tail: bytes = b"") -> LoadedProgram:
    exe = parse_mz(path)
    mem = Memory()
    create_psp(mem, psp_segment, command_tail)
    load_segment = (psp_segment + 0x10) & 0xFFFF
    mem.load(load_segment, 0, exe.load_module)

    # Apply relocations. LZEXE-packed targets may have zero relocations, but this
    # is kept here so the loader remains correct if we later swap in another build.
    for r in exe.relocations:
        value = mem.rw(load_segment + r.segment, r.offset)
        mem.ww(load_segment + r.segment, r.offset, (value + load_segment) & 0xFFFF)

    return LoadedProgram(
        exe=exe,
        memory=mem,
        psp_segment=psp_segment,
        load_segment=load_segment,
        entry_cs=(load_segment + exe.header.cs) & 0xFFFF,
        entry_ip=exe.header.ip,
        initial_ss=(load_segment + exe.header.ss) & 0xFFFF,
        initial_sp=exe.header.sp,
        overlay=exe.overlay,
    )
