from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import struct


@dataclass(frozen=True)
class Relocation:
    offset: int
    segment: int


@dataclass(frozen=True)
class MZHeader:
    last_page_bytes: int
    pages: int
    relocations: int
    header_paragraphs: int
    min_extra_paragraphs: int
    max_extra_paragraphs: int
    ss: int
    sp: int
    checksum: int
    ip: int
    cs: int
    relocation_table_offset: int
    overlay_number: int

    @property
    def exe_image_size(self) -> int:
        size = self.pages * 512
        if self.last_page_bytes:
            size -= 512 - self.last_page_bytes
        return size

    @property
    def header_size(self) -> int:
        return self.header_paragraphs * 16


@dataclass(frozen=True)
class MZExecutable:
    path: Path
    header: MZHeader
    load_module: bytes
    relocations: tuple[Relocation, ...]
    overlay: bytes

    @property
    def entry_cs_ip(self) -> tuple[int, int]:
        return self.header.cs, self.header.ip

    @property
    def stack_ss_sp(self) -> tuple[int, int]:
        return self.header.ss, self.header.sp


def parse_mz(path: str | Path) -> MZExecutable:
    p = Path(path)
    data = p.read_bytes()
    if len(data) < 28 or data[:2] != b"MZ":
        raise ValueError(f"{p} is not a DOS MZ executable")
    hvals = struct.unpack_from("<14H", data, 0)
    header = MZHeader(*hvals[1:])
    if header.header_size > len(data):
        raise ValueError(f"MZ header extends past EOF in {p}")
    image_size = min(header.exe_image_size, len(data))
    load_module = data[header.header_size:image_size]
    overlay = data[image_size:]
    relocs: list[Relocation] = []
    for i in range(header.relocations):
        off = header.relocation_table_offset + i * 4
        if off + 4 > len(data):
            raise ValueError(f"relocation table is truncated in {p}")
        r_off, r_seg = struct.unpack_from("<HH", data, off)
        relocs.append(Relocation(r_off, r_seg))
    return MZExecutable(p, header, load_module, tuple(relocs), overlay)
