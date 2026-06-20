"""Prehistorik 2 frame renderer — recovered native logic (tile-row draw).

Status: recovered; verification target ``pre2/probes/verify_frame.py``.
Merge target: the frame renderer → eventually ``update_frame()``.

This recovers the tile-row draw at ``1030:346E``: it paints one 20-tile row of the
scrolling background by reading tile indices from the level :class:`TileMap`,
OR-accumulating each tile's attribute flags, and compositing the tile through the
**already-recovered, verified** blit (:func:`pre2.recovered.renderer.blit_sprite`).

Per the island-composition rule, this calls ``blit_sprite`` directly rather than
returning to ASM: the blit's verified contract (the A000 planar framebuffer plus a
``di += 2`` advance) is exactly the side effect ``346E`` relies on from its
``call 3B69``, so no contact point with the original ASM is needed here.

All state is plain data (planes as byte buffers, tables as ``bytes``); the VM↔memory
translation lives in ``pre2/bridge/frame.py``.
"""
from __future__ import annotations

from dataclasses import dataclass

from pre2.recovered.oracle import oracle_link
from pre2.recovered.renderer import ROW_STRIDE, WRAP_AT, WRAP_SPAN, blit_sprite

__all__ = ["RowFlags", "VISIBLE_COLS", "RING_COLS", "BG_PTR_BIAS", "draw_tile_row"]

VISIBLE_COLS = 0x14      # tiles drawn per row (cx=0x14 in 346E)
RING_COLS = 0x14         # column ring modulus; di wraps back one screen row at it
BG_PTR_BIAS = 0x7E80     # [0x2DF2] = di + 0x7E80 (background-restore source base)


@dataclass
class RowFlags:
    """The three attribute flags ``346E`` OR-accumulates across the tiles it draws.

    These mirror the in-memory accumulators (which are *not* reset per row):
    ``plane_attr`` ⇄ ``[0x6BB9]``, ``tile_flags`` ⇄ ``[0x2DEE]``,
    ``tile_type`` ⇄ ``[0x2DF0]``. Seed them with the pre-call memory values so the
    result matches the original's running OR.
    """

    plane_attr: int = 0
    tile_flags: int = 0
    tile_type: int = 0


@oracle_link("1030:346E",
             "A000 framebuffer (one 20-tile row) + OR-accumulated [0x6BB9]/[0x2DEE]/[0x2DF0]; di preserved",
             "VERIFIED")
def draw_tile_row(planes, tilemap, tile_offset, di, scroll_src, col_ring,
                  fine_scroll, blit_type, mask_region, flags=None):
    """Recover ``1030:346E`` — draw one 20-tile background row.

    Mutates ``planes`` (the four A000 EGA planes) and ``flags``; returns
    ``(internal_di, flags)``. NOTE: ``346E`` push/pops ``di`` (3471/34E8), so it
    *preserves* the caller's ``di`` — the returned ``internal_di`` is the loop's
    scratch pointer (useful for tests), **not** a value the caller reads back. The
    caller-visible contract is: the framebuffer + the OR-accumulated ``flags``.

    * ``tile_offset`` — tilemap byte offset of the row's first tile
      (``camera_y * TILEMAP_STRIDE + camera_x``; the ASM's ``si = ax``).
    * ``di`` — screen destination offset for this row (the ASM's incoming ``di``).
    * ``scroll_src`` — ``[0x2DB6]`` added to ``di`` once at entry.
    * ``col_ring`` — ``[0x2DE4]`` column ring index (the ``dx`` start).
    * ``fine_scroll`` — ``[0x6BC0]``; ``bg_off = bg_ptr - ROW_STRIDE * fine_scroll``.
    * ``blit_type`` — 256-entry sprite-type table (``1A13:0x4DF4``) for the blit.
    * ``mask_region`` — the transparency-mask bytes (``1A13:0x2DF4`` onward); the
      mask for type ``t`` is ``mask_region[(t-2)*0x20 : (t-2)*0x20 + 0x20]``.
    """
    if flags is None:
        flags = RowFlags()
    bg_ptr = (di + BG_PTR_BIAS) & 0xFFFF          # [asm 3476/347A]
    di = (di + scroll_src) & 0xFFFF                # [asm 3487: add di,[0x2DB6]]
    dx = col_ring                                  # [asm 348B: mov dx,[0x2DE4]]
    si = tile_offset & 0xFFFF                      # [asm 3485: mov si,ax]

    for _ in range(VISIBLE_COLS):
        if di >= WRAP_AT:                          # [asm 3496: cmp di,0x5D40 / sub di,0x1E00]
            di = (di - WRAP_SPAN) & 0xFFFF
        tile = tilemap.tiles[si]                   # [asm 34A0: lodsb]
        si = (si + 1) & 0xFFFF
        flags.plane_attr |= tilemap.plane_attr[tile]  # [asm 34A3-34A8] -> [0x6BB9]
        flags.tile_flags |= tilemap.tile_flags[tile]  # [asm 34AD-34B4] -> [0x2DEE]
        flags.tile_type |= tilemap.tile_type[tile]    # [asm 34B9-34C0] -> [0x2DF0]

        typ = blit_type[tile]                      # blit reads 1A13:[0x4DF4+idx]
        mask = b""
        if typ >= 2:
            off = (typ - 2) * 0x20
            mask = mask_region[off:off + 0x20]
        bg_off = (bg_ptr - ROW_STRIDE * fine_scroll) & 0xFFFF
        blit_sprite(planes, tile, di, typ, bg_off, mask)  # [asm 34CD: call 3B69]
        di = (di + 2) & 0xFFFF                     # blit's di += 2 contract
        bg_ptr = (bg_ptr + 2) & 0xFFFF             # [asm 34D1: add [0x2DF2],2]

        dx += 1                                    # [asm 34D7: inc dx]
        if dx >= RING_COLS:                        # [asm 34D8-34E0: ring column wrap]
            di = (di - ROW_STRIDE) & 0xFFFF
            dx = 0
    return di, flags
