"""Read DOSBox-X save states (``.sav``) — conventional memory + CPU registers.

A DOSBox-X save state is a ZIP of zlib-compressed components. We use two:

* ``Memory`` — the full RAM image (a small fixed header, then raw bytes from
  physical address 0). The conventional first 1 MB is all a real-mode DOS game
  uses.
* ``CPU`` — the CPU POD. The 8 general registers, ``ip`` and ``flags`` sit at
  fixed offsets at the very start: ``eax,ecx,edx,ebx,esp,ebp,esi,edi`` (x86
  order) as 32-bit values at offset 0, ``ip`` at +0x20, ``flags`` at +0x28.
  (Validated on a PRE2 save: the register frame the game pushes onto its stack
  matches these byte-for-byte.)

The *segment* registers are not stored in a generically locatable form, so the
caller derives them from the loaded image — see
``pre2.runtime.load_dosbox_savestate``, which finds the program by a code
signature and works out CS/DS/SS/ES from it.
"""
from __future__ import annotations

import struct
import zipfile
import zlib
from pathlib import Path

CONV_SIZE = 0x100000  # 1 MB conventional memory


def _component(path: str | Path, name: str) -> bytes:
    with zipfile.ZipFile(path) as z:
        raw = z.read(name)
    try:
        return zlib.decompress(raw)
    except zlib.error:
        return raw  # some builds store a component uncompressed


def is_dosbox_savestate(path: str | Path) -> bool:
    """True if ``path`` looks like a DOSBox(-X) save state (a ZIP with Memory+CPU)."""
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
        return "Memory" in names and "CPU" in names
    except (zipfile.BadZipFile, OSError):
        return False


def read_memory_image(path: str | Path) -> bytes:
    """Return the full decompressed RAM image (including any small leading header)."""
    return _component(path, "Memory")


def read_gpr(path: str | Path) -> dict[str, int]:
    """Return ``{ax,bx,cx,dx,si,di,bp,sp,ip,flags}`` (16-bit) from the CPU component."""
    c = _component(path, "CPU")

    def u16(off: int) -> int:
        return struct.unpack_from("<H", c, off)[0]

    return {
        "ax": u16(0x00), "cx": u16(0x04), "dx": u16(0x08), "bx": u16(0x0C),
        "sp": u16(0x10), "bp": u16(0x14), "si": u16(0x18), "di": u16(0x1C),
        "ip": u16(0x20), "flags": u16(0x28),
    }


def locate_conventional(mem_image: bytes, signature: bytes, sig_offset: int) -> tuple[int, int]:
    """Find the program in ``mem_image`` by a known code ``signature``.

    ``signature`` is bytes that appear at ``code_segment:sig_offset``. The DOSBox-X
    Memory component starts at physical address 0 (NO header — physical == dump
    offset), so the returned ``header_len`` is always 0. The program may sit a few
    bytes higher inside its segment than ours (an LZEXE-unpacking artifact); that
    shift is consistent across DOSBox's own code/data/registers, so flooring the
    match position into a paragraph recovers the correct code segment regardless.
    Returns ``(header_len=0, code_segment)``.
    """
    pos = mem_image.find(signature)
    if pos < 0:
        raise ValueError("program code signature not found in the save state's memory")
    return 0, (pos - sig_offset) >> 4


def conventional_memory(mem_image: bytes, header_len: int) -> bytearray:
    """Slice the conventional 1 MB out of the raw dump given the detected header."""
    return bytearray(mem_image[header_len:header_len + CONV_SIZE])
