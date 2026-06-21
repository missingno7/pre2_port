"""Prehistorik 2 sprite-sheet decode — recovered native logic (the *pure* layer).

Status: VERIFIED (byte-for-byte against the original ASM, load-time witness).

PRE2 renders sprites from a *planar VRAM cache* (``0xA000:0x5E80``, 256 slots of
32 bytes) that the per-frame blit copies to the screen. The cache is filled at
level load by demultiplexing a decompressed sprite sheet into the four EGA bit
planes. This module is the pure transform that does that demux; the VM↔memory
translation lives in ``pre2/bridge/sprites.py`` and the adapter in
``pre2/replacements.py``.

Original routines (segment ``1030``; see ``docs/pre2/symbol_ledger.md``):

* ``4316`` — local bank: for each of 256 slots, read a ``u16`` ``code`` from the
  sheet's index table; if ``code < 0x100`` copy the sprite's 4 planes (32 B each)
  into the cache slot via the Sequencer map mask; otherwise leave the slot blank.
* ``4389`` — shared/union bank: same demux for ``0x100 <= code < 0x200`` from a
  second sprite bank.

A sprite is 16×16 pixels at 1 bit/plane = 4 planes × 32 bytes = 128 bytes. In the
cache, a slot's four planes live at the *same* 32-byte offset (the original
overlays them through the map mask); here they are kept as four parallel plane
buffers, matching the EGA hardware planes.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pre2.islands import oracle_link

__all__ = [
    "NUM_SLOTS",
    "PLANES",
    "SLOT_BYTES",
    "SPRITE_BYTES",
    "PIXEL_BASE",
    "LOCAL_CODE_MAX",
    "SENTINEL_CODE",
    "demux_sprite",
    "SpriteSheet",
    "SharedSpriteBank",
    "SpriteCache",
    "decode_sprite_cache",
]

NUM_SLOTS = 256          # index-table entries / cache slots
PLANES = 4              # EGA bit planes
SLOT_BYTES = 0x20        # 32 bytes per plane per sprite (16x16, 1bpp)
SPRITE_BYTES = PLANES * SLOT_BYTES  # 128 bytes per sprite (4 planes)
PIXEL_BASE = 0x200       # sheet pixel data starts after the 256-entry index table
LOCAL_CODE_MAX = 0x100   # code < this -> local bank; otherwise the shared bank
SENTINEL_CODE = 0xFFFF   # unused slot: the original reads a wrapped (garbage) address


def demux_sprite(sprite: bytes) -> list[bytes]:
    """Split a 128-byte sprite into its four 32-byte EGA planes.

    The sheet stores a sprite as four consecutive 32-byte planes; the original
    writes each plane to the cache through the Sequencer map mask (``[asm 434C]``).
    """
    return [sprite[p * SLOT_BYTES: (p + 1) * SLOT_BYTES] for p in range(PLANES)]


@dataclass(frozen=True)
class SpriteSheet:
    """A decompressed sprite sheet: 256-entry ``u16`` index table + pixel data.

    ``index_table[slot]`` is the ``code`` for that slot; the local sprite pixels
    live at ``PIXEL_BASE + code * SPRITE_BYTES`` (``[asm 4346: shl si,7]``).
    """

    index_table: tuple[int, ...]
    pixel_data: bytes  # the sheet bytes from PIXEL_BASE onward

    @classmethod
    def from_bytes(cls, data: bytes) -> "SpriteSheet":
        codes = tuple(data[2 * i] | (data[2 * i + 1] << 8) for i in range(NUM_SLOTS))
        return cls(index_table=codes, pixel_data=data[PIXEL_BASE:])

    def local_sprite(self, code: int) -> bytes:
        # [asm 4346: shl si,7 / 4348: add si,0x200] -> pixel_data is PIXEL_BASE-relative.
        off = code * SPRITE_BYTES
        return self.pixel_data[off: off + SPRITE_BYTES]


@dataclass(frozen=True)
class SharedSpriteBank:
    """The shared/union sprite bank (``4389``); sprite ``code`` at ``(code-0x100)*128``.

    The original addresses this bank by *segment arithmetic*
    (``[asm 4389: shl ax,3; add ax,[0x2DD8]]`` -> ``((code-0x100)*8 + base) & 0xFFFF``),
    so an out-of-range / sentinel code wraps to a garbage address. In this pure
    model the bank is a flat buffer; :meth:`has_sprite` reports whether a code is a
    real in-bank sprite (the only slots that carry game-meaningful pixels).
    """

    data: bytes

    def has_sprite(self, code: int) -> bool:
        off = (code - LOCAL_CODE_MAX) * SPRITE_BYTES
        return code != SENTINEL_CODE and 0 <= off and off + SPRITE_BYTES <= len(self.data)

    def shared_sprite(self, code: int) -> bytes:
        off = (code - LOCAL_CODE_MAX) * SPRITE_BYTES
        return self.data[off: off + SPRITE_BYTES]


@dataclass
class SpriteCache:
    """The planar VRAM sprite cache: ``PLANES`` parallel plane buffers.

    Each plane is ``NUM_SLOTS * SLOT_BYTES`` bytes; slot ``s`` plane ``p`` lives at
    ``planes[p][s*SLOT_BYTES : +SLOT_BYTES]`` (the original overlays all planes at
    one VRAM offset via the map mask).
    """

    planes: list[bytearray] = field(
        default_factory=lambda: [bytearray(NUM_SLOTS * SLOT_BYTES) for _ in range(PLANES)]
    )

    def set_slot(self, slot: int, planes: list[bytes]) -> None:
        off = slot * SLOT_BYTES
        for p in range(PLANES):
            self.planes[p][off: off + SLOT_BYTES] = planes[p]

    def slot(self, slot: int, plane: int) -> bytes:
        off = slot * SLOT_BYTES
        return bytes(self.planes[plane][off: off + SLOT_BYTES])


@oracle_link("1030:4316",
             "planar sprite cache (0xA000:0x5E80) demuxed from the sheet + shared bank; "
             "also 1030:4389 (shared codes >= 0x100)",
             "VERIFIED", merge_target="sprite pipeline")
def decode_sprite_cache(
    sheet: SpriteSheet,
    shared_bank: SharedSpriteBank | None = None,
    cache: SpriteCache | None = None,
) -> SpriteCache:
    """Demux a sprite sheet (+ shared bank) into the planar cache.

    Faithful translation of ``4316`` (local: ``code < 0x100``) followed by ``4389``
    (shared: ``code >= 0x100``). Sentinel/out-of-bank codes select no real sprite
    and are left untouched here — the original copies wrapped garbage into them,
    but those slots are unused (never blitted) so they carry no game-meaningful
    pixels. The live replacement reproduces the wrapped bytes exactly by reading VM
    memory (see ``pre2/bridge/sprites.py``); this pure model decodes the meaningful
    sprites only.
    """
    if cache is None:
        cache = SpriteCache()
    for slot, code in enumerate(sheet.index_table):
        if code < LOCAL_CODE_MAX:
            cache.set_slot(slot, demux_sprite(sheet.local_sprite(code)))
        elif shared_bank is not None and shared_bank.has_sprite(code):
            cache.set_slot(slot, demux_sprite(shared_bank.shared_sprite(code)))
        # else: sentinel / out-of-bank unused slot, left unchanged.
    return cache
