"""Prehistorik 2 frame renderer — recovered native logic.

Status: VERIFIED (byte-exact vs the original ASM under in-VM lockstep); verification
targets ``pre2/probes/verify_{frame,grid,scroll,panel}.py`` (348D / 35A1 / 3A27 /
3054). Merge target: the frame renderer → eventually ``update_frame()``.

Recovers two draws that paint the scrolling tile background by reading tile indices
from the level :class:`TileMap`, OR-accumulating attribute flags, and compositing
through the **already-recovered, verified** blit
(:func:`pre2.recovered.renderer.blit_sprite`):

* :func:`draw_tile_row` (``1030:348D``) — one 20-tile row (the incremental
  scroll-fill); draws every tile.
* :func:`draw_grid` (``1030:35A1``) — the full 12×20 visible grid redraw, guarded
  by a prev-camera/dirty check, drawing only the non-opaque (type≥1) tiles (the
  opaque type-0 background comes from the scrolled buffer).

Per the island-composition rule, this calls ``blit_sprite`` directly rather than
returning to ASM: the blit's verified contract (the A000 planar framebuffer plus a
``di += 2`` advance) is exactly the side effect ``348D`` relies on from its
``call 3B88``, so no contact point with the original ASM is needed here.

All state is plain data (planes as byte buffers, tables as ``bytes``); the VM↔memory
translation lives in ``pre2/bridge/frame.py``.
"""
from __future__ import annotations

from dataclasses import dataclass

from pre2.islands import oracle_link
from pre2.recovered.renderer import ROW_STRIDE, WRAP_AT, WRAP_SPAN, blit_sprite

__all__ = [
    "RowFlags", "GridResult", "VISIBLE_COLS", "VISIBLE_ROWS", "RING_COLS",
    "BG_PTR_BIAS", "draw_tile_row", "draw_grid", "scroll_copy", "panel_copy",
    "calc_scroll_source", "redraw_animated_grid",
]


class AnimGridUnsupported(Exception):
    """Raised when ``redraw_animated_grid`` meets a non-type-0 tile — the original only
    ever blits opaque (type-0) animated tiles here and does *not* maintain a background
    pointer for the masked path, so a masked tile is unrecovered (fail loud, never guess)."""

SCROLL_WRAP_SRC = 0x3F40   # source offset of the ring-buffer wrap section (3A27)
SCREEN_ROW = 0x28          # bytes per screen row (the per-row source back-step)
SCROLL_HEIGHT = 0xB0       # visible scroll height the row split is measured against

VISIBLE_COLS = 0x14      # tiles drawn per row (cx=0x14 in 348D/35A1)
VISIBLE_ROWS = 0x0C      # tile rows in the visible grid (ch=0x0C in 35A1)
RING_COLS = 0x14         # column ring modulus; di wraps back one screen row at it
BG_PTR_BIAS = 0x7E80     # [0x2DF6] = di + 0x7E80 (background-restore source base)
# 35A1 per-row advances after the 20-tile inner loop (continuous across the grid):
GRID_SI_ROW_ADVANCE = 0xEC    # si += 0xEC (+0x14 already consumed -> +0x100 stride)
GRID_DI_ROW_ADVANCE = 0x280   # di += 0x280 to the next screen row
GRID_BG_ROW_ADVANCE = 0x258   # [0x2DF6] += 0x258 (+0x28 already consumed -> +0x280)


@dataclass
class RowFlags:
    """The three attribute flags ``348D`` OR-accumulates across the tiles it draws.

    These mirror the in-memory accumulators (which are *not* reset per row):
    ``plane_attr`` ⇄ ``[0x6BBD]``, ``tile_flags`` ⇄ ``[0x2DF2]``,
    ``tile_type`` ⇄ ``[0x2DF4]``. Seed them with the pre-call memory values so the
    result matches the original's running OR.
    """

    plane_attr: int = 0
    tile_flags: int = 0
    tile_type: int = 0


@oracle_link("1030:348D",
             "A000 framebuffer (one 20-tile row) + OR-accumulated [0x6BBD]/[0x2DF2]/[0x2DF4]; di preserved",
             "VERIFIED", merge_target="render_frame")
def draw_tile_row(planes, tilemap, tile_offset, di, scroll_src, col_ring,
                  fine_scroll, blit_type, mask_region, flags=None):
    """Recover ``1030:348D`` — draw one 20-tile background row.

    Mutates ``planes`` (the four A000 EGA planes) and ``flags``; returns
    ``(internal_di, flags)``. NOTE: ``348D`` push/pops ``di`` (3471/34E8), so it
    *preserves* the caller's ``di`` — the returned ``internal_di`` is the loop's
    scratch pointer (useful for tests), **not** a value the caller reads back. The
    caller-visible contract is: the framebuffer + the OR-accumulated ``flags``.

    * ``tile_offset`` — tilemap byte offset of the row's first tile
      (``camera_y * TILEMAP_STRIDE + camera_x``; the ASM's ``si = ax``).
    * ``di`` — screen destination offset for this row (the ASM's incoming ``di``).
    * ``scroll_src`` — ``[0x2DB6]`` added to ``di`` once at entry.
    * ``col_ring`` — ``[0x2DE4]`` column ring index (the ``dx`` start).
    * ``fine_scroll`` — ``[0x6BC4]``; ``bg_off = bg_ptr - ROW_STRIDE * fine_scroll``.
    * ``blit_type`` — 256-entry sprite-type table (``1A0F:0x4DF8``) for the blit.
    * ``mask_region`` — the transparency-mask bytes (``1A0F:0x2DF8`` onward); the
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
        flags.plane_attr |= tilemap.plane_attr[tile]  # [asm 34A3-34A8] -> [0x6BBD]
        flags.tile_flags |= tilemap.tile_flags[tile]  # [asm 34AD-34B4] -> [0x2DF2]
        flags.tile_type |= tilemap.tile_type[tile]    # [asm 34B9-34C0] -> [0x2DF4]

        typ = blit_type[tile]                      # blit reads 1A0F:[0x4DF8+idx]
        mask = b""
        if typ >= 2:
            off = (typ - 2) * 0x20
            mask = mask_region[off:off + 0x20]
        bg_off = (bg_ptr - ROW_STRIDE * fine_scroll) & 0xFFFF
        blit_sprite(planes, tile, di, typ, bg_off, mask)  # [asm 34CD: call 3B88]
        di = (di + 2) & 0xFFFF                     # blit's di += 2 contract
        bg_ptr = (bg_ptr + 2) & 0xFFFF             # [asm 34D1: add [0x2DF6],2]

        dx += 1                                    # [asm 34D7: inc dx]
        if dx >= RING_COLS:                        # [asm 34D8-34E0: ring column wrap]
            di = (di - ROW_STRIDE) & 0xFFFF
            dx = 0
    return di, flags


@dataclass
class GridResult:
    """Outcome of :func:`draw_grid`, mirroring 35A1's caller-visible side effects."""

    redrew: bool          # whether the grid was actually redrawn (else early-exit)
    prev_x: int           # new [0x2DE0] (prev camera X)
    prev_y: int           # new [0x2DE2] (prev camera Y)
    dirty: int            # new [0x2DF4]
    dirty_rows: int       # new [0x2DF5]
    tile_flags: int       # new [0x2DF2] (OR over all grid tiles, when redrawn)


@oracle_link("1030:35A1",
             "A000 framebuffer (visible-grid type>=1 tiles) + [0x2DF2]/[0x2DF4]/[0x2DF5] + prev camera "
             "[0x2DE0]/[0x2DE2]; di/regs preserved",
             "VERIFIED", merge_target="render_frame")
def draw_grid(planes, tilemap, camera_x, camera_y, prev_x, prev_y, dirty, dirty_rows,
              scroll_src, col_ring, fine_scroll, blit_type, mask_region):
    """Recover ``1030:35A1`` — the full 12x20 visible-grid redraw.

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
    tile_flags_acc = 0                             # [asm 35CB] [0x2DF2] reset, then OR
    new_dirty = 0                                  # [asm 35C5] [0x2DF4] reset, set 1 below
    si = (camera_y * 0x100 + camera_x) & 0xFFFF    # [asm 35CE-35D5] si = ah:al
    di = scroll_src & 0xFFFF                        # [asm 35DB] di = [0x2DB6]
    bg_ptr = BG_PTR_BIAS                            # [asm 35DF] [0x2DF6] = 0x7E80

    for _row in range(VISIBLE_ROWS):               # [asm 35E5] ch = 0x0C
        dx = col_ring                              # [asm 35E5] dx = [0x2DE4] (per row)
        for _col in range(VISIBLE_COLS):           # [asm 35E9] cl = 0x14
            tile = tilemap.tiles[si]               # [asm 35EB] bl = es:[si]
            tile_flags_acc |= tilemap.tile_flags[tile]  # [asm 35EE-35F2] -> [0x2DF2]
            typ = blit_type[tile]                  # [asm 35F6] [0x4DF8+bx]
            if typ >= 1:                           # [asm 35FB] jb skips type 0
                new_dirty = 1                      # [asm 35FD] [0x2DF4] = 1
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


def _copy_run(planes, si, di, n):
    """Latched 4-plane copy of ``n`` bytes (es:di <- ds:si); returns (si, di)."""
    for _ in range(n):
        for p in range(4):
            planes[p][di] = planes[p][si]
        si = (si + 1) & 0xFFFF
        di = (di + 1) & 0xFFFF
    return si, di


@oracle_link("1030:3A27",
             "A000 planar scroll-copy of the visible window (ring buffer -> display page) "
             "+ all-plane clear of the leading strip; bx/di/si/ds/es preserved",
             "VERIFIED", merge_target="render_frame")
def scroll_copy(planes, scroll_src, dest, col_ring, fine_scroll, row_ring, row_factor):
    """Recover ``1030:3A27`` — the vertical-scroll screen copy.

    A write-mode-1 latched 4-plane block copy (helper 452F) of the visible window
    from the scroll ring buffer (``[0x2DB6]``) to the display page (``[0x2DD4]``).
    Each row is split into ``dl`` + ``dh`` byte segments around the column ring
    (``[0x2DE4]``) with a one-row source back-step; the copy runs over ``bp`` main
    rows then ``bx`` rows from the ring wrap (``0x3F40``). Finally the leading
    ``S = 0x28*row_factor`` strip at ``dest`` is cleared on all planes. Operates on
    the four EGA planes directly (cf. :func:`renderer.restore_background`).
    """
    s = (SCREEN_ROW * row_factor) & 0xFFFF          # [asm 3A12] S = 0x28 * [0x6BF4]
    si = scroll_src & 0xFFFF                         # [asm 3A1C]
    di = (dest + s) & 0xFFFF                          # [asm 3A20/3A35] di = [0x2DD4] + S
    dh = (col_ring << 1) & 0xFF                       # [asm 3A26/3A2E]
    dl = ((0x14 - col_ring) << 1) & 0xFF              # [asm 3A24/3A2A/3A2C] dl + dh == 0x28

    # row-count split between the main copy and the ring-wrap copy [asm 3A3A-3A68]
    bp = (0xC0 - fine_scroll - (row_ring << 4)) & 0xFFFF
    if bp >= SCROLL_HEIGHT:                           # [asm 3A4A: cmp bp,0xB0 / jb]
        bp = (SCROLL_HEIGHT - row_factor) & 0xFFFF
        bx = 0
    else:
        bx = (SCROLL_HEIGHT - bp - row_factor) & 0xFFFF
        if bx & 0x8000:                               # [asm 3A64: jns] negative -> fold into bp
            bp = (bp + bx) & 0xFFFF
            bx = 0

    si = (si + SCREEN_ROW * fine_scroll) & 0xFFFF     # [asm 3A6A] si += 0x28 * fine
    if bp == 0:                                       # [asm 3A7E-3A82] xchg bp,bx
        bp, bx = bx, bp

    for _ in range(bp):                               # [asm 3A84-3A91] main rows
        si, di = _copy_run(planes, si, di, dl)
        si = (si - SCREEN_ROW) & 0xFFFF
        si, di = _copy_run(planes, si, di, dh)
        si = (si + SCREEN_ROW) & 0xFFFF

    if bx:                                            # [asm 3A93-3AAB] ring-wrap rows
        si = (SCROLL_WRAP_SRC + dh) & 0xFFFF
        for _ in range(bx):
            si, di = _copy_run(planes, si, di, dl)
            si = (si - SCREEN_ROW) & 0xFFFF
            si, di = _copy_run(planes, si, di, dh)
            si = (si + SCREEN_ROW) & 0xFFFF

    # clear the leading strip on all four planes [asm 3AB2-3ACB]: S>>1 words at dest
    words = s >> 1
    for k in range(words * 2):
        off = (dest + k) & 0xFFFF
        for p in range(4):
            planes[p][off] = 0


@oracle_link("1030:3054",
             "A000 page-flip copy: back page [0x2DD4] -> front page [0x2DD2] (4-plane, "
             "0xB0-row 2-byte strips); regs preserved (vsync wait is timing-only)",
             "VERIFIED", merge_target="render_frame")
def panel_copy(planes, src_page, dst_page):
    """Recover ``1030:3054`` — the double-buffer page-flip copy.

    Copies 2-byte-wide x 0xB0-row vertical strips (write-mode-1 latched 4-plane
    copy, screen stride 0x28) from the back page (``[0x2DD4]``) to the front page
    (``[0x2DD2]``), at the symmetric columns ``0x14-2k`` and ``0x14+2k`` for
    ``k=0..9`` (the original interleaves these with vsync waits to flip tear-free;
    the wait is timing-only and carries no pixel contract, so it is omitted).
    """
    rows = SCROLL_HEIGHT                              # cx = 0xB0 (176) per strip
    for k in range(10):                               # [asm 304B-3076] 0x3031 = 0,4,..,0x24
        field_3033 = (0x14 - 2 * k) & 0xFFFF
        for col in (field_3033, (field_3033 + 4 * k) & 0xFFFF):  # two 307C calls
            si = (col + src_page) & 0xFFFF            # [asm 3084] si = di + [0x2DD4]
            di = (col + dst_page) & 0xFFFF            # [asm 3088] di = di + [0x2DD2]
            for _ in range(rows):                     # [asm 3096-309E] movsb x2 then +0x26
                for c in range(2):
                    for p in range(4):
                        planes[p][(di + c) & 0xFFFF] = planes[p][(si + c) & 0xFFFF]
                si = (si + SCREEN_ROW) & 0xFFFF
                di = (di + SCREEN_ROW) & 0xFFFF


@oracle_link("1030:3588",
             "compute the scroll-copy source offset into the tile ring buffer: "
             "[0x2DBA] = 2*camera_col + 0x280*camera_row + 0x3F40 (16-bit)",
             "ASM_MATCHED", merge_target="render_frame")
def calc_scroll_source(camera_col, camera_row):
    """Recover ``1030:3588-359A`` — the scroll-copy source pointer.

    Returns the offset stored in ``[0x2DBA]``: ``2*camera_col + 0x280*camera_row +
    0x3F40`` (mod 0x10000). ``camera_row`` is the byte ``[0x2DEA]``; the ASM builds the
    ``0x280*row`` term as ``(row<<9) + (row<<7)`` via the dx/bx shifts.
    """
    ax = (camera_col << 1) & 0xFFFF                   # [asm 3588: shl ax,1]
    dx = (camera_row << 8) & 0xFFFF                   # [asm 358A-358E: dh=[2DEA], dl=0]
    bx = (dx << 1) & 0xFFFF                           # [asm 3592: shl bx,1]  (row<<9)
    dx = (dx >> 1) & 0xFFFF                           # [asm 3594: shr dx,1]  (row<<7)
    return (ax + dx + bx + SCROLL_WRAP_SRC) & 0xFFFF  # [asm 3596-359A: +0x3F40]


@oracle_link("1030:3668",
             "A000 framebuffer (the animated background tiles only) + [0x2DF2] (OR of the "
             "type table over the whole grid) + [0x6BBD] (any-drawn flag). Redraws the "
             "12x20 visible grid but blits only tiles flagged in the 0x6988 table, each "
             "remapped through the current animation frame [0x6BC2]; di/regs preserved.",
             "ASM_MATCHED", merge_target="render_frame")
def redraw_animated_grid(planes, tiles, type_tbl, flag_tbl, anim_xlat, blit_type,
                         camera_col, camera_row, fine_col, scroll_dest):
    """Recover ``1030:36B3-3715`` — redraw the animated background tiles.

    Walks the 12x20 visible grid (same ring buffer as :func:`draw_grid`): for every tile
    it ORs ``type_tbl[tile]`` into the accumulated flags, and **only where**
    ``flag_tbl[tile] != 0`` (the animated tiles) it remaps the tile through the current
    animation frame (``anim_xlat[tile]`` = ``[[0x6BC2] + tile]``) and blits it opaque.

    Returns ``(tile_flags_acc, any_drawn)`` (the ``[0x2DF2]`` / ``[0x6BBD]`` contract).
    The throttle + animation-frame advance (3668-36A6) is the thin controller that
    supplies ``anim_xlat``; here ``anim_xlat`` is already the selected frame's 256-byte
    remap slice. Every animated tile observed is type 0 (opaque); a non-type-0 tile is
    unrecovered (the ASM keeps no background pointer here) and fails loud.
    """
    tile_flags_acc = 0                                 # [asm 36A9] [0x2DF2] reset, then OR
    any_drawn = 0                                      # [asm 36AE] [0x6BBD] reset
    si = (camera_row * 0x100 + camera_col) & 0xFFFF    # [asm 36B3-36BA] si = ah:al
    di = scroll_dest & 0xFFFF                          # [asm 36C0] di = [0x2DBA]

    for _row in range(VISIBLE_ROWS):                   # [asm 36C4] ch = 0x0C
        dx = fine_col                                  # [asm 36C4] dx = [0x2DE8] (per row)
        for _col in range(VISIBLE_COLS):               # [asm 36C8] cl = 0x14
            tile = tiles[si]                           # [asm 36CA] bl = es:[si]
            tile_flags_acc |= type_tbl[tile]           # [asm 36CD-36D1] -> [0x2DF2]
            if flag_tbl[tile] != 0:                    # [asm 36D5] draw only flagged tiles
                any_drawn = 1                          # [asm 36DC] [0x6BBD] = 1
                remapped = anim_xlat[tile]             # [asm 36E1-36E7] xlat via [0x6BC2]
                if blit_type[remapped] != 0:           # 3B88 derives type from [0x4DF8]
                    raise AnimGridUnsupported(
                        f"animated tile {tile} -> {remapped} is type "
                        f"{blit_type[remapped]} (only type-0 recovered)")
                if di >= WRAP_AT:                      # [asm 36EA] pre-blit ring wrap
                    di = (di - WRAP_SPAN) & 0xFFFF
                blit_sprite(planes, remapped, di, 0, 0)  # [asm 36F4 -> 3B88] opaque blit
            dx += 1                                    # [asm 36F9] inc dx
            if dx >= RING_COLS:                        # [asm 36FA-3702] row-buffer ring
                di = (di - ROW_STRIDE) & 0xFFFF
                dx = 0
            di = (di + 2) & 0xFFFF                      # [asm 3704] per-tile (blit nets +2)
            si = (si + 1) & 0xFFFF                      # [asm 3706] next tile
        di = (di + GRID_DI_ROW_ADVANCE) & 0xFFFF        # [asm 370B] +0x280 next screen row
        si = (si + GRID_SI_ROW_ADVANCE) & 0xFFFF        # [asm 370F] +0xEC next tilemap row

    return tile_flags_acc & 0xFF, any_drawn
