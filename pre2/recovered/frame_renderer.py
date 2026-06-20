"""Prehistorik 2 frame renderer — recovered native logic.

Status: recovered; verification targets ``pre2/probes/verify_frame.py`` (346E) and
``pre2/probes/verify_grid.py`` (3582). Merge target: the frame renderer →
eventually ``update_frame()``.

Recovers two draws that paint the scrolling tile background by reading tile indices
from the level :class:`TileMap`, OR-accumulating attribute flags, and compositing
through the **already-recovered, verified** blit
(:func:`pre2.recovered.renderer.blit_sprite`):

* :func:`draw_tile_row` (``1030:346E``) — one 20-tile row (the incremental
  scroll-fill); draws every tile.
* :func:`draw_grid` (``1030:3582``) — the full 12×20 visible grid redraw, guarded
  by a prev-camera/dirty check, drawing only the non-opaque (type≥1) tiles (the
  opaque type-0 background comes from the scrolled buffer).

Per the island-composition rule, this calls ``blit_sprite`` directly rather than
returning to ASM: the blit's verified contract (the A000 planar framebuffer plus a
``di += 2`` advance) is exactly the side effect ``346E`` relies on from its
``call 3B69``, so no contact point with the original ASM is needed here.

All state is plain data (planes as byte buffers, tables as ``bytes``); the VM↔memory
translation lives in ``pre2/bridge/frame.py``.
"""
from __future__ import annotations

from dataclasses import dataclass

from pre2.islands import oracle_link
from pre2.recovered.renderer import ROW_STRIDE, WRAP_AT, WRAP_SPAN, blit_sprite

__all__ = [
    "RowFlags", "GridResult", "VISIBLE_COLS", "VISIBLE_ROWS", "RING_COLS",
    "BG_PTR_BIAS", "draw_tile_row", "draw_grid",
]

VISIBLE_COLS = 0x14      # tiles drawn per row (cx=0x14 in 346E/3582)
VISIBLE_ROWS = 0x0C      # tile rows in the visible grid (ch=0x0C in 3582)
RING_COLS = 0x14         # column ring modulus; di wraps back one screen row at it
BG_PTR_BIAS = 0x7E80     # [0x2DF2] = di + 0x7E80 (background-restore source base)
# 3582 per-row advances after the 20-tile inner loop (continuous across the grid):
GRID_SI_ROW_ADVANCE = 0xEC    # si += 0xEC (+0x14 already consumed -> +0x100 stride)
GRID_DI_ROW_ADVANCE = 0x280   # di += 0x280 to the next screen row
GRID_BG_ROW_ADVANCE = 0x258   # [0x2DF2] += 0x258 (+0x28 already consumed -> +0x280)


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
             "VERIFIED", merge_target="frame renderer")
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


@dataclass
class GridResult:
    """Outcome of :func:`draw_grid`, mirroring 3582's caller-visible side effects."""

    redrew: bool          # whether the grid was actually redrawn (else early-exit)
    prev_x: int           # new [0x2DDC] (prev camera X)
    prev_y: int           # new [0x2DDE] (prev camera Y)
    dirty: int            # new [0x2DF0]
    dirty_rows: int       # new [0x2DF1]
    tile_flags: int       # new [0x2DEE] (OR over all grid tiles, when redrawn)


@oracle_link("1030:3582",
             "A000 framebuffer (visible-grid type>=1 tiles) + [0x2DEE]/[0x2DF0]/[0x2DF1] + prev camera "
             "[0x2DDC]/[0x2DDE]; di/regs preserved",
             "VERIFIED", merge_target="frame renderer")
def draw_grid(planes, tilemap, camera_x, camera_y, prev_x, prev_y, dirty, dirty_rows,
              scroll_src, col_ring, fine_scroll, blit_type, mask_region):
    """Recover ``1030:3582`` — the full 12x20 visible-grid redraw.

    Guarded by a prev-camera/dirty check (the early-exit path); on redraw, draws
    only the non-opaque (type>=1) tiles via the recovered blit, accumulates
    ``tile_flags`` over *all* tiles, and updates the dirty flags + prev camera.
    Returns a :class:`GridResult`; ``di`` and the other registers are preserved by
    the ASM (push/pop), so nothing register-side is part of the contract.
    """
    # --- dirty / early-exit decision (mirrors 3590-35B9, incl. the prev stores) ---
    # The compares use the OLD prev_x/prev_y; the stores update them to the camera
    # only on the dirty_rows == 0 path (and prev_y only if camera_x matched).
    if dirty_rows != 0:                            # [asm 3590] rows scrolled -> redraw
        redraw = True                              # (prev camera left unchanged here)
        new_prev_x, new_prev_y = prev_x, prev_y
    else:
        new_prev_x = camera_x                      # [asm 359E] store prev_x = cam_x
        if camera_x != prev_x:                     # [asm 35A1] -> 35B2 (prev_y not stored)
            new_prev_y = prev_y
            redraw = dirty != 0                    # [asm 35B2]
        else:
            new_prev_y = camera_y                  # [asm 35AA] store prev_y = cam_y
            redraw = dirty != 0 if camera_y != prev_y else False  # [asm 35AD/35B2/35AF]

    if not redraw:                                 # [asm jmp 363c] early-exit
        return GridResult(False, new_prev_x, new_prev_y, dirty, dirty_rows, 0)

    # --- redraw the 12x20 grid (mirrors 35C3-363A) ---
    tile_flags_acc = 0                             # [asm 35CB] [0x2DEE] reset, then OR
    new_dirty = 0                                  # [asm 35C5] [0x2DF0] reset, set 1 below
    si = (camera_y * 0x100 + camera_x) & 0xFFFF    # [asm 35CE-35D5] si = ah:al
    di = scroll_src & 0xFFFF                        # [asm 35DB] di = [0x2DB6]
    bg_ptr = BG_PTR_BIAS                            # [asm 35DF] [0x2DF2] = 0x7E80

    for _row in range(VISIBLE_ROWS):               # [asm 35E5] ch = 0x0C
        dx = col_ring                              # [asm 35E5] dx = [0x2DE4] (per row)
        for _col in range(VISIBLE_COLS):           # [asm 35E9] cl = 0x14
            tile = tilemap.tiles[si]               # [asm 35EB] bl = es:[si]
            tile_flags_acc |= tilemap.tile_flags[tile]  # [asm 35EE-35F2] -> [0x2DEE]
            typ = blit_type[tile]                  # [asm 35F6] [0x4DF4+bx]
            if typ >= 1:                           # [asm 35FB] jb skips type 0
                new_dirty = 1                      # [asm 35FD] [0x2DF0] = 1
                if di >= WRAP_AT:                  # [asm 3604-360A]
                    di = (di - WRAP_SPAN) & 0xFFFF
                mask = mask_region[(typ - 2) * 0x20:(typ - 2) * 0x20 + 0x20] if typ >= 2 else b""
                bg_off = (bg_ptr - ROW_STRIDE * fine_scroll) & 0xFFFF
                blit_sprite(planes, tile, di, typ, bg_off, mask)  # [asm 360E call 3B5C]
            dx += 1                                # [asm 3613]
            if dx >= RING_COLS:                    # [asm 3614-361C] ring column wrap
                di = (di - ROW_STRIDE) & 0xFFFF
                dx = 0
            bg_ptr = (bg_ptr + 2) & 0xFFFF         # [asm 361E]
            di = (di + 2) & 0xFFFF                  # [asm 3623]
            si = (si + 1) & 0xFFFF                  # [asm 3625]
        bg_ptr = (bg_ptr + GRID_BG_ROW_ADVANCE) & 0xFFFF  # [asm 362A]
        di = (di + GRID_DI_ROW_ADVANCE) & 0xFFFF          # [asm 3630]
        si = (si + GRID_SI_ROW_ADVANCE) & 0xFFFF          # [asm 3634]

    return GridResult(True, new_prev_x, new_prev_y, new_dirty, 0, tile_flags_acc & 0xFF)
