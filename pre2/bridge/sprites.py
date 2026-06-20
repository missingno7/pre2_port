"""Memory views for the sprite-decode island (VM memory ⇄ recovered dataclasses).

The one place that knows *where* the sprite sheet, shared bank, and planar VRAM
cache live in PRE2 memory. Gameplay decisions live in
``pre2/recovered/sprite_decode.py``; this module only translates layout.

Layout (see ``docs/pre2/symbol_ledger.md``):

* data segment ``1A13`` holds the source-segment selector words and the index
  table copy;
* the sprite sheet is a decompressed ``.SQZ`` asset at a computed segment;
* the planar VRAM cache is the four EGA shadow planes at offset ``0x5E80``.
"""
from __future__ import annotations

from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE

from pre2.recovered.sprite_decode import (
    LOCAL_CODE_MAX,
    NUM_SLOTS,
    PLANES,
    PIXEL_BASE,
    SLOT_BYTES,
    SPRITE_BYTES,
    SharedSpriteBank,
    SpriteCache,
    SpriteSheet,
    demux_sprite,
)

DATA_SEG = 0x1A13
CACHE_OFF = 0x5E80                 # planar cache base within each EGA plane
CACHE_PLANE_BYTES = NUM_SLOTS * SLOT_BYTES  # 0x2000 per plane

# data-segment word/byte variables used by the decode setup (1030:42F7)
VAR_LOCAL_BASE = 0x2DD6            # [0x2DD6] sheet base segment seed
VAR_BANK_SELECT = 0x2D86          # [0x2D86] index into the paragraph table
VAR_BANK_TABLE = 0x2D2C           # [bank_select + 0x2D2C] paragraph multiplier
VAR_SHARED_BASE = 0x2DD8          # [0x2DD8] shared/union bank base segment
VAR_INDEX_COPY = 0x25CA           # copy of the 256-entry index table


def _rb(mem, seg: int, off: int) -> int:
    return mem.data[((seg << 4) + off) & 0xFFFFF]


def _rw(mem, seg: int, off: int) -> int:
    base = ((seg << 4) + off) & 0xFFFFF
    return mem.data[base] | (mem.data[base + 1] << 8)


def sprite_sheet_segment(mem) -> int:
    """Compute the local sprite-sheet segment as ``42F7`` does.

    ``src = [0x2DD6] + ([ [0x2D86] + 0x2D2C ] << 4)``  (``[asm 42F7-430C]``).
    """
    base = _rw(mem, DATA_SEG, VAR_LOCAL_BASE)
    select = _rb(mem, DATA_SEG, VAR_BANK_SELECT)
    multiplier = _rb(mem, DATA_SEG, VAR_BANK_TABLE + select)
    return (base + (multiplier << 4)) & 0xFFFF


def shared_bank_segment(mem) -> int:
    return _rw(mem, DATA_SEG, VAR_SHARED_BASE)


def read_sprite_sheet(mem, seg: int) -> SpriteSheet:
    base = (seg << 4) & 0xFFFFF
    # generous: index table (0x200) + up to 256 local sprites of pixel data.
    end = base + PIXEL_BASE + NUM_SLOTS * SPRITE_BYTES
    return SpriteSheet.from_bytes(bytes(mem.data[base:end]))


def read_shared_bank(mem, seg: int) -> SharedSpriteBank:
    base = (seg << 4) & 0xFFFFF
    return SharedSpriteBank(bytes(mem.data[base:base + 0x10000]))


def _plane_base(plane: int) -> int:
    return EGA_APERTURE + plane * EGA_PLANE_STRIDE + CACHE_OFF


def read_sprite_cache(mem) -> SpriteCache:
    """Read the four planar cache planes out of the EGA shadow VRAM."""
    planes = []
    for p in range(PLANES):
        b = _plane_base(p)
        planes.append(bytearray(mem.data[b:b + CACHE_PLANE_BYTES]))
    return SpriteCache(planes=planes)


def write_sprite_cache(mem, cache: SpriteCache) -> None:
    """Write a decoded cache back into the EGA shadow planes (replacement path)."""
    for p in range(PLANES):
        b = _plane_base(p)
        mem.data[b:b + CACHE_PLANE_BYTES] = cache.planes[p]


# ---- live per-slot decode (the replacement path for 42F7 / 436A) -------------
# Returns ``{slot: [4 planes]}`` maps so the adapter can either write them (hybrid)
# or diff them against the ASM result (verify) without touching VRAM twice.

def index_table_copy(mem, seg: int) -> bytes:
    """The 256-entry index table raw bytes (``[asm 431F: rep movsw -> 0x25CA]``)."""
    base = (seg << 4) & 0xFFFFF
    return bytes(mem.data[base:base + PIXEL_BASE])


def shared_index_codes(mem) -> list[int]:
    """The codes the shared pass reads from the ``[0x25CA]`` index copy."""
    base = (DATA_SEG << 4) + VAR_INDEX_COPY
    return [mem.data[base + 2 * i] | (mem.data[base + 2 * i + 1] << 8) for i in range(NUM_SLOTS)]


def compute_local_slots(mem, src_seg: int) -> dict[int, list[bytes]]:
    """Slots the local pass (``42F7``) writes: ``code < 0x100`` from the sheet."""
    sheet = read_sprite_sheet(mem, src_seg)
    out: dict[int, list[bytes]] = {}
    for slot, code in enumerate(sheet.index_table):
        if code < LOCAL_CODE_MAX:
            out[slot] = demux_sprite(sheet.local_sprite(code))
    return out


def _shared_sprite_segment(base: int, code: int) -> int:
    # [asm 4389: shl ax,3; 438D: add ax,[0x2DD8]] -> ((code-0x100)*8 + base) & 0xFFFF.
    return ((code - LOCAL_CODE_MAX) * 8 + base) & 0xFFFF


def compute_shared_slots(mem, shared_base: int) -> dict[int, list[bytes]]:
    """Slots the shared pass (``436A``) writes: ``code >= 0x100``.

    The source is addressed by the original's segment arithmetic and read straight
    from VM memory, so out-of-bank / sentinel codes reproduce the same wrapped
    bytes the ASM copies — byte-exact, no special cases.
    """
    out: dict[int, list[bytes]] = {}
    for slot, code in enumerate(shared_index_codes(mem)):
        if code >= LOCAL_CODE_MAX:
            seg = _shared_sprite_segment(shared_base, code)
            b = (seg << 4) & 0xFFFFF
            out[slot] = demux_sprite(bytes(mem.data[b:b + SPRITE_BYTES]))
    return out


def write_slots(mem, slot_map: dict[int, list[bytes]]) -> None:
    for slot, planes in slot_map.items():
        off = slot * SLOT_BYTES
        for p in range(PLANES):
            b = _plane_base(p) + off
            mem.data[b:b + SLOT_BYTES] = planes[p]


def read_slot(mem, slot: int) -> list[bytes]:
    off = slot * SLOT_BYTES
    return [bytes(mem.data[_plane_base(p) + off: _plane_base(p) + off + SLOT_BYTES])
            for p in range(PLANES)]


# ---- whole-plane views (the per-frame blit/renderer operates on these) --------

def plane_views(mem) -> list[memoryview]:
    """Writable views of the four EGA planes (writes land in shadow VRAM)."""
    mv = memoryview(mem.data)
    return [mv[EGA_APERTURE + p * EGA_PLANE_STRIDE: EGA_APERTURE + (p + 1) * EGA_PLANE_STRIDE]
            for p in range(PLANES)]


def snapshot_planes(mem) -> list[bytearray]:
    """A detached copy of the four EGA planes (for verify-mode oracle compares)."""
    return [bytearray(mem.data[EGA_APERTURE + p * EGA_PLANE_STRIDE: EGA_APERTURE + (p + 1) * EGA_PLANE_STRIDE])
            for p in range(PLANES)]
