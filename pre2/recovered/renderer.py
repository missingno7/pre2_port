"""Prehistorik 2 sprite blit / background restore — recovered native logic.

Status: VERIFIED (byte-for-byte against the original ASM, per-blit witness).

The per-frame draw renders each sprite/tile from the planar VRAM cache
(``0xA000:0x5E80``, filled by ``pre2.recovered.sprite_decode``) onto the screen,
dispatching on the sprite's transparency class. That class — the type table
``[0x4DF8]`` and the partial-sprite masks ``[0x2DF8]`` — is produced by the
classifier at ``1030:4232``, which is **still ASM** (not recovered): this recovered
blit only *consumes* its output.

* **type 0 — opaque:** plain 4-plane copy of the 16×16 sprite to the screen
  (``1030:3B9B``).
* **type 1 — empty:** draw nothing; just restore the background into the slot
  (``1030:3D84``).
* **type ≥2 — partial:** restore the background, then composite the sprite with
  transparency — ``screen = (screen AND mask) OR sprite`` (``1030:3BF6``: GC
  func=AND phase then plane-by-plane OR phase).

All three operate on the four EGA bit planes as parallel byte buffers; the VM↔plane
translation lives in ``pre2/bridge/sprites.py``. A sprite is 16 rows of 2 bytes
(16×16 px), written at screen stride ``0x28`` with the background's vertical wrap.
"""
from __future__ import annotations

from pre2.islands import oracle_link

__all__ = [
    "ROW_STRIDE",
    "ROWS",
    "SPRITE_WIDTH",
    "CACHE_BASE",
    "SLOT_BYTES",
    "WRAP_AT",
    "WRAP_SPAN",
    "dest_rows",
    "blit_opaque",
    "restore_background",
    "blit_masked",
    "blit_sprite",
]

ROW_STRIDE = 0x28        # screen bytes per sprite row (40 = 320px/8)
ROWS = 16               # sprite height
SPRITE_WIDTH = 2         # bytes per row (16 px)
CACHE_BASE = 0x5E80      # planar sprite cache base within each plane
SLOT_BYTES = 0x20        # 32 bytes per cache slot
WRAP_AT = 0x5D40         # the scrolled background buffer wraps vertically here
WRAP_SPAN = 0x1E00       # ... by this many bytes (192 rows), a circular buffer


def dest_rows(di: int):
    """Yield ``(row, offset)`` for each of the 16 sprite rows, applying the
    background buffer's vertical wrap (``[asm 3D7E: cmp di,0x5D40; sub di,0x1E00]``)."""
    d = di & 0xFFFF
    for r in range(ROWS):
        if d >= WRAP_AT:
            d = (d - WRAP_SPAN) & 0xFFFF
        yield r, d
        d = (d + ROW_STRIDE) & 0xFFFF


def blit_opaque(planes: list[bytearray], idx: int, di: int) -> None:
    """Type 0: copy the 16×16 sprite from the cache to the screen, all 4 planes."""
    src = CACHE_BASE + idx * SLOT_BYTES
    for r, d in dest_rows(di):
        for p in range(4):
            for c in range(SPRITE_WIDTH):
                planes[p][d + c] = planes[p][src + r * SPRITE_WIDTH + c]


def restore_background(planes: list[bytearray], di: int, bg_off: int) -> None:
    """Type 1 (and the first phase of type ≥2): copy the scrolled background into
    the slot (``1030:3D84``). The source advances linearly; the dest wraps."""
    for r, d in dest_rows(di):
        s = (bg_off + r * ROW_STRIDE) & 0xFFFF
        for p in range(4):
            for c in range(SPRITE_WIDTH):
                planes[p][d + c] = planes[p][s + c]


def blit_masked(planes: list[bytearray], idx: int, di: int, bg_off: int, mask: bytes) -> None:
    """Type ≥2: restore the background, then composite ``(bg AND mask) OR sprite``.

    ``mask`` is the classifier's transparency mask (32 bytes, ``bit=1`` where the
    sprite pixel is transparent), so ``bg AND mask`` keeps the background only at
    transparent pixels and the ``OR sprite`` fills the opaque ones.
    """
    restore_background(planes, di, bg_off)
    src = CACHE_BASE + idx * SLOT_BYTES
    for r, d in dest_rows(di):
        for p in range(4):
            for c in range(SPRITE_WIDTH):
                k = r * SPRITE_WIDTH + c
                planes[p][d + c] = ((mask[k] & planes[p][d + c]) | planes[p][src + k]) & 0xFF


@oracle_link("1030:3B88", "A000 planar framebuffer (one 16x16 slot); di += 2", "VERIFIED",
             merge_target="renderer")
def blit_sprite(planes: list[bytearray], idx: int, di: int, sprite_type: int,
                bg_off: int, mask: bytes = b"") -> None:
    """Dispatch one sprite blit on its transparency class (``1030:3B88``)."""
    if sprite_type == 0:
        blit_opaque(planes, idx, di)
    elif sprite_type == 1:
        restore_background(planes, di, bg_off)
    else:
        blit_masked(planes, idx, di, bg_off, mask)
