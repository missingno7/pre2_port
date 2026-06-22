"""Prehistorik 2 moving-sprite renderer — recovered native logic (pure).

Recovers the hot per-frame sprite engine at ``1030:26FA`` (the dominant gameplay
draw path, ~78% of interpreted instructions — distinct from the dormant tile-aligned
``653D``/``blit_sprite`` path). It walks the **active-sprite list** and, for each live
sprite, animates it, computes its camera-relative screen position, culls/clips it
against the visible window, and emits a **draw command** for the planar blit.

Two layers, recovered separately:
* **Phase A (this pass): the driver.** Pure list-walk → cull → position → clip →
  :class:`SpriteDraw` commands. No VRAM, no VGA. Verified against the ASM's
  per-sprite decisions (screen offset, byte-width, rows, shift, source pointer).
* **Phase B: the blit.** Consume :class:`SpriteDraw` and paint the de-interleaved
  EGA plane buffers (opaque / shifted-masked modes), modelled in software so the
  VGA latch/GC dance collapses to per-plane writes.

Pure: no ``cpu``/``mem``/``dos_re`` imports. Memory layout lives in
``pre2/bridge/object_render.py``; the per-sprite attribute tables and the active
list are passed in as plain values.
"""
from __future__ import annotations

from dataclasses import dataclass

from pre2.islands import oracle_link

__all__ = [
    "RECORD_BYTES", "LIST_TOP", "LIST_BASE", "SCREEN_W", "SCREEN_H",
    "Sprite", "SpriteAttr", "Camera", "SpriteDraw", "SpriteRecordUpdate",
    "plan_sprite", "plan_frame", "plan_record_update",
]

RECORD_BYTES = 0x12      # 18 bytes per active-sprite record [asm 2DE4: sub si,0x12]
LIST_TOP = 0x5720        # cursor starts here [asm 270C: mov si,0x5720]
LIST_BASE = 0x4F0A       # stop when cursor < this [asm 2DE7: cmp si,0x4F0A]
SCREEN_W = 0x140         # 320 px [asm 27A6: cmp ax,0x140]
SCREEN_H = 0xB0          # 176 visible rows [asm 27F0: cmp ax,0xB0]
TILE_PX = 16             # camera is in tiles; *16 -> pixels [asm 2796: shl cx,4]


@dataclass(frozen=True)
class Sprite:
    """One active-sprite record (18 bytes)."""
    x: int          # [+0] world X (px)
    y: int          # [+2] world Y (px)
    sprite_id: int  # [+4] sprite id (0xFFFF = empty slot)
    flags: int      # [+5] flags (bit5 set after draw)
    life: int       # [+0x11] anim/life counter (decremented; blink-gated)


@dataclass(frozen=True)
class SpriteAttr:
    """Per-sprite-id attributes (looked up by id, all indexed id<<1)."""
    width: int      # low byte of [0x7190] word — sprite width in source bytes (pre-shift)
    height: int     # high byte of [0x7190] word — sprite height in rows
    x_off: int      # [0x752A] signed draw X offset (px)
    y_off: int      # [0x752B] signed draw Y offset (px)
    src_seg: int    # [0x62E8] sprite pixel-data segment
    src_off: int    # [0x5F48] sprite pixel-data offset


@dataclass(frozen=True)
class Camera:
    """The frame's camera / scroll inputs the renderer reads from DGROUP."""
    cam_x: int          # [0x2DE4] camera X in tiles
    cam_y: int          # [0x2DE6] camera Y in tiles
    fine_scroll: int    # [0x6BC4] sub-tile vertical fine scroll (px)
    row_factor: int     # [0x6BF8] vertical world->screen bias
    dest_page: int      # [0x2DD8] active display-page base offset
    row_stride: int     # [0x2DB0] screen byte stride per row (40)
    global_shift: int   # cs:[0] global pixel-shift divisor (>>) applied to widths
    frame: int          # [0x6BD5] frame counter (post-increment) — drives the blink phase


# blit mode [asm 26F7]: 0 = blink-off (mask/erase only), 1 = normal (mask+sprite),
# 0x10 = opaque (sprite-only, monochrome all-plane OR).
MODE_ERASE = 0x00
MODE_NORMAL = 0x01
MODE_OPAQUE = 0x10


@dataclass(frozen=True)
class SpriteDraw:
    """A planned blit (Phase A output; Phase B paints it)."""
    sprite_id: int
    src_seg: int
    src_off: int        # source pointer offset (incl. top-clip skip)
    dest_off: int       # VRAM byte offset (incl. display page + screen X>>3)
    byte_width: int     # bytes per row to write (post-shift, clipped)
    rows: int           # rows to write (clipped)
    shift: int          # sub-byte pixel shift = screen_x & 7
    flipped: bool       # H-flip
    mode: int           # blit mode (MODE_*)
    clipped: bool       # left/right edge clip -> the [asm 2CEA] partial-edge blit variant
    src_bw: int         # source row width (= byte_width + left_skip + right_skip)
    left_skip: int      # [26ED] source bytes skipped at the left edge per row
    right_skip: int     # [26EF] source bytes skipped at the right edge per row
    right_clipped: bool # right edge clipped (sets the [26F0] final-carry mask)
    full_rows: int      # original (pre top/bottom-clip) height — the source plane-block stride
                        # is full_rows*src_bw (the source holds full-height sprites)


@dataclass(frozen=True)
class SpriteRecordUpdate:
    """The per-frame mutation 26FA applies to an active-sprite record (the side effect
    distinct from the pixels): the saturating-decremented life [+0x11] and the new flags
    byte [+5] (drawn bit cleared each frame, then set iff the sprite was actually drawn)."""
    new_life: int       # [+0x11] = (life-1) saturating  [asm 2742..2746]
    new_flags: int      # [+5]    drawn bit (0x20) cleared [2732] then set+0xBF [28B6/28BA]


def plan_record_update(spr: "Sprite", drawn: bool) -> SpriteRecordUpdate:
    """The record mutation for one processed (non-empty) sprite. [asm 2732/2742/28B6]

    Applied to *every* non-empty record the list walk reaches: the drawn bit (0x20) is
    cleared and life is decremented (saturating) regardless; the drawn bit is then set
    (and bit6 cleared) only when the sprite produced a blit. Equivalent to the ASM's
    split pre-plan/post-plan writes, collapsed to the final record state (nothing reads
    the record between them)."""
    new_life = (spr.life - 1) & 0xFF if spr.life else 0       # [asm 2742] saturating dec
    flags = spr.flags & 0xDF                                   # [asm 2732] clear drawn bit
    if drawn:
        flags = (flags | 0x20) & 0xBF                          # [asm 28B6 or 0x20 / 28BA and 0xBF]
    return SpriteRecordUpdate(new_life=new_life, new_flags=flags)


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _s8(v: int) -> int:
    v &= 0xFF
    return v - 0x100 if v & 0x80 else v


def plan_sprite(spr: Sprite, attr: SpriteAttr, cam: Camera) -> SpriteDraw | None:
    """Plan one sprite's blit, or ``None`` if it is culled. [asm 271C..28BE]

    Faithful to the ASM: H-flip mirrors the X offset, position is camera-relative
    (X in px, Y biased by ``row_factor`` and ``fine_scroll``), the sprite is culled
    against [0,320)×(0,176) with a left-edge width test, and left/right/top/bottom
    overruns clip the byte-width / rows / source-skip and shift the dest offset.
    """
    if spr.sprite_id == 0xFFFF:                         # [asm 2713: cmp [si+4],-1] empty slot
        return None
    flipped = bool(spr.sprite_id & 0x8000)             # [asm 2739 shl / 273B rcl cs:[26e2]] H-flip = id bit15

    # --- blink/anim mode [asm 2740..2761] ---
    # The decremented life is a *local* used only for the blink/mode decision below; the
    # actual [si+0x11] write-back is the checkpoint's record-mutation contract, not a
    # field of the draw command.
    life_after = (spr.life - 1) & 0xFF if spr.life else 0   # [asm 2742 sub/2746 adc] saturating dec
    bit14 = bool(spr.sprite_id & 0x4000)               # [asm 2757 test bh,0x80] -> id bit14
    if life_after == 0 or (cam.frame & 3) == 0:        # expired, or blink "on" 1/4 frames
        mode = MODE_OPAQUE if bit14 else MODE_NORMAL
    else:                                              # [asm 2753] blink off
        mode = MODE_ERASE

    width, height = attr.width, attr.height
    x_off = attr.x_off
    if flipped:                                          # [asm 2775: al = width - x_off]
        x_off = width - x_off
    # --- screen X (px) [asm 277A..27B5] ---
    screen_x = _s16(spr.x - x_off - cam.cam_x * TILE_PX)
    if screen_x >= SCREEN_W:                             # [asm 27A6] off right
        return None
    if _s16(screen_x + width) < 0:                       # [asm 27AE..27B3] off left
        return None
    # --- screen Y: ``screen_y`` is the sprite's BASELINE (bottom); the top row is
    # ``screen_y - height`` [asm 27B8..27F5; 27EB sub al,dh]. ---
    screen_y = _s16(spr.y + _s8(attr.y_off) + cam.row_factor
                    - (cam.cam_y * TILE_PX + cam.fine_scroll))
    if screen_y <= 0:                                    # [asm 27D9: jg] baseline must be on-screen
        return None
    top_row = _s16(screen_y - height)                   # [asm 27EB] dest's top row
    if top_row >= SCREEN_H:                             # [asm 27F0: cmp ax,0xB0] entirely below
        return None

    byte_width = width >> cam.global_shift               # [asm 27FC: shr dl,cs:[0]]
    rows = height                                       # [asm 26EC seed = dh]
    full_rows = height                                  # source plane-block stride = full_rows*src_bw
    src_skip = 0

    if top_row < 0:                                     # top clip [asm 2811..2826]
        clip = -top_row
        rows -= clip
        src_skip = clip * byte_width                    # [asm 281C: mul dl -> source skip]
        dest_row = 0                                    # [asm 2824: xor di,di]
    else:
        dest_row = top_row
    if screen_y > SCREEN_H:                             # bottom clip [asm 2849..2858]
        rows -= (screen_y - SCREEN_H)

    # dest VRAM offset: top_row*stride + display page, then the horizontal column.
    dest_off = (dest_row * cam.row_stride + cam.dest_page) & 0xFFFF   # [asm 2828/282E]
    col = screen_x >> 3                                  # arithmetic (sar) [asm 2865/287E]
    src_bw = byte_width                                  # source row width (full sprite)
    left_skip = right_skip = 0
    clipped = right_clipped = False
    if col >= 0:                                         # [asm 287E: screen_x >= 0]
        dest_off = (dest_off + col) & 0xFFFF            # [asm 2885: add [26f1], col]
        end_col = col + byte_width - cam.row_stride      # right clip [asm 288A..28B1]
        if end_col >= 0:                                 # col+bw >= stride -> partial-edge blit
            clipped = right_clipped = True
            right_skip = end_col                         # [asm 28AD: [26ef]=overflow]
            byte_width -= end_col                        # [asm 28A3: sub [26e4],overflow]
    else:                                                # [asm 2865: screen_x < 0] left clip
        left_skip = -col                                 # [asm 2871: [26ed]=-col]
        byte_width += col                                # [asm 286C: add [26e4], col(neg)]
        clipped = True

    shift = screen_x & 7                                 # [asm 28C8: cl = [26ea]&7]
    src_off = (attr.src_off + src_skip) & 0xFFFF         # [asm 283F: [26f3]=[bx+5F48]+skip]
    # The ASM does NOT cull on byte_width: a left-clip sliver can have byte_width==0
    # (only the partial carry byte is drawn). It only culls via the screen-window tests.
    if rows <= 0 or byte_width < 0:
        return None
    return SpriteDraw(sprite_id=spr.sprite_id, src_seg=attr.src_seg, src_off=src_off,
                      dest_off=dest_off, byte_width=byte_width, rows=rows,
                      shift=shift, flipped=flipped, mode=mode,
                      clipped=clipped, src_bw=src_bw, left_skip=left_skip,
                      right_skip=right_skip, right_clipped=right_clipped, full_rows=full_rows)


@oracle_link("1030:26FA",
             "render the active-sprite list to A000 planar VRAM (cull/animate/position/clip "
             "+ shifted-masked planar blit, incl H-flip); mutates object-record flags + life "
             "+ frame counter",
             "VERIFIED", merge_target="frame renderer")
def plan_frame(sprites, attrs, cam: Camera):
    """Walk the active list top->down and emit a draw command per visible sprite.

    Phase A: returns the ordered ``list[SpriteDraw]``. (Phase B paints them.)
    ``sprites`` is the list of :class:`Sprite` from LIST_TOP-RECORD_BYTES down to
    LIST_BASE; ``attrs`` maps sprite_id -> :class:`SpriteAttr`.
    """
    draws = []
    for spr in sprites:
        if spr.sprite_id == 0xFFFF:
            continue
        attr = attrs.get(spr.sprite_id)
        if attr is None:
            continue
        d = plan_sprite(spr, attr, cam)
        if d is not None:
            draws.append(d)
    return draws


# --------------------------------------------------------------------------- #
# Phase B — the planar blit. Models the VGA write path the ASM drives via the
# Graphics Controller: a CPU byte V written at offset o with data-rotate ``shift``
# and logical function ``func`` becomes ``plane[p][o] = func(ror8(V,shift),
# plane[p][o])`` for each map-mask-selected plane. So the latch/sequencer dance
# collapses to per-plane reads/writes on de-interleaved plane buffers.
# --------------------------------------------------------------------------- #

def ror8(v: int, r: int) -> int:
    r &= 7
    v &= 0xFF
    return v if r == 0 else ((v >> r) | (v << (8 - r))) & 0xFF


def _bit_reverse(v: int) -> int:
    r = 0
    for b in range(8):
        if v & (1 << b):
            r |= 1 << (7 - b)
    return r


# horizontal pixel mirror within a byte (the ASM's CS:0x2F34 xlat table) [asm 2944]
BITREV = bytes(_bit_reverse(i) for i in range(256))


def _phase(planes, map_mask: int, dest_off: int, src: bytes, si: int,
           draw: SpriteDraw, stride: int, *, invert: bool, op_and: bool,
           final_mask: int) -> None:
    """One blit phase over the map-masked planes. ``invert`` writes ``~byte`` (the
    AND-mask phase); ``op_and`` selects AND vs OR. Shift-carry (``ch``/``bh``/``ah``)
    spreads each source byte across the byte boundary, mirrored by the GC rotate.
    Per row it skips ``left_skip`` / ``right_skip`` source bytes (edge clip) and masks
    the trailing carry byte with ``final_mask``. [asm 2C4F/2C70/2BDD/2C00 + 2CEA clip]"""
    bw, rows, shift = draw.byte_width, draw.rows, draw.shift
    ch = (0xFF << shift) & 0xFF                          # edge mask [asm 28D2]
    bh = (~ch) & 0xFF                                    # carry mask [asm 28EA]
    di = dest_off
    for _r in range(rows):
        ah = 0                                           # carry reset per row [asm 2C4D xor ax]
        d = di
        if draw.left_skip:                               # [asm 2CEA] skip+prime at the left edge
            si += draw.left_skip - 1
            ah = src[si] & bh; si += 1
        for _c in range(bw):
            s = src[si]; si += 1                         # [asm lodsb]
            al = ((s & ch) | ah) & 0xFF                  # [asm and al,ch / or al,ah]
            ah = s & bh                                  # spill to next byte [asm and bl,bh]
            rv = ror8((~al) & 0xFF if invert else al, shift)
            for p in range(4):
                if map_mask & (1 << p):
                    planes[p][d] = (planes[p][d] & rv) if op_and else (planes[p][d] | rv)
            d = (d + 1) & 0xFFFF
        base = (~ah) & 0xFF if invert else ah            # trailing carry byte [asm xchg [di],ah]
        fc = (base | final_mask) if op_and else (base & final_mask)
        rv = ror8(fc, shift)
        for p in range(4):
            if map_mask & (1 << p):
                planes[p][d] = (planes[p][d] & rv) if op_and else (planes[p][d] | rv)
        si += draw.right_skip                            # [asm 2D1A/2DB4 add si,[26ef]]
        di = (di + stride) & 0xFFFF


def _phase_flip(planes, map_mask: int, dest_off: int, src: bytes, block_base: int,
                draw: SpriteDraw, stride: int, *, invert: bool, op_and: bool,
                final_mask: int) -> None:
    """Flipped (H-mirror) counterpart of :func:`_phase` [asm 2915..2A0E + clip 2AA1].
    Each row is read right-to-left from the full source-row end and every byte is
    bit-reversed (``BITREV`` = CS:0x2F34), so the sprite is mirrored; the dest advances
    left-to-right with the same shift-carry. Handles the left-edge clip prime + the
    right-edge final-carry mask, so it covers both the plain and clipped (2AA1) flips."""
    bw, rows, shift, src_bw = draw.byte_width, draw.rows, draw.shift, draw.src_bw
    ls = draw.left_skip
    ch = (0xFF << shift) & 0xFF
    bh = (~ch) & 0xFF
    start = src_bw - 1 - ls                               # first source col read (from the row end)
    di = dest_off
    for r in range(rows):
        row = block_base + r * src_bw
        ah = (BITREV[src[row + src_bw - ls]] & bh) if ls else 0   # left-edge prime [asm 2AB4]
        d = di
        for c in range(bw):
            s = BITREV[src[row + start - c]]             # reversed read + bit-mirror [asm AC/2E D7]
            al = ((s & ch) | ah) & 0xFF
            ah = s & bh
            rv = ror8((~al) & 0xFF if invert else al, shift)
            for p in range(4):
                if map_mask & (1 << p):
                    planes[p][d] = (planes[p][d] & rv) if op_and else (planes[p][d] | rv)
            d = (d + 1) & 0xFFFF
        base = (~ah) & 0xFF if invert else ah
        fc = (base | final_mask) if op_and else (base & final_mask)
        rv = ror8(fc, shift)
        for p in range(4):
            if map_mask & (1 << p):
                planes[p][d] = (planes[p][d] & rv) if op_and else (planes[p][d] | rv)
        di = (di + stride) & 0xFFFF


def paint_sprite(planes, draw: SpriteDraw, src: bytes, stride: int) -> None:
    """Paint one planned sprite onto the four plane buffers. ``src`` is the sprite's
    pixel data from ``draw.src_off`` (block-sequential by source row width ``src_bw``:
    a mask block then 4 plane blocks for masked modes, or one block for opaque).
    H-flipped sprites use the mirrored phase."""
    block = draw.src_bw * draw.full_rows                 # source plane stride is full-height
    and_mask = 0xFF if draw.right_clipped else 0x00      # [asm 2D10 or ah,[26f0]]
    or_mask = 0x00 if draw.right_clipped else 0xFF       # [asm 2DA7 and ah,[26f0]] ([26f0] toggled)
    phase = _phase_flip if draw.flipped else _phase      # [asm 290A test [26e2],1 / jne 2915]
    if draw.mode == MODE_OPAQUE:                         # [asm 2C99: al=0xF] one block, all planes, OR
        phase(planes, 0x0F, draw.dest_off, src, 0, draw, stride,
              invert=False, op_and=False, final_mask=or_mask)
        return
    # mask (AND) phase: block 0 -> all planes [asm 2C4F / 2BDD]
    phase(planes, 0x0F, draw.dest_off, src, 0, draw, stride,
          invert=True, op_and=True, final_mask=and_mask)
    if draw.mode == MODE_NORMAL:                         # sprite (OR) phase: blocks 1..4 per plane [asm 2C70]
        for p in range(4):
            phase(planes, 1 << p, draw.dest_off, src, (p + 1) * block, draw, stride,
                  invert=False, op_and=False, final_mask=or_mask)
