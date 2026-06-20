"""Prehistorik 2 object draw — recovered native logic (renderer-facing).

Status: recovered; verification target ``pre2/probes/verify_object.py``.
Merge target: the frame renderer.

Recovers the per-object sprite draw at ``1030:6544`` — the shared draw-command unit
that every object draw goes through. Given an object's tile position and sprite
index it culls against the visible camera window, computes the screen destination
offset, and composites the sprite via the **already-recovered, verified** blit
(:func:`pre2.recovered.renderer.blit_sprite`) — recovered → recovered, no ASM
contact point. This is renderer-facing only; object *update* (movement / AI /
collision, where the player/enemy sprites draw themselves) is a separate, later island.

All state is plain data; the VM↔memory translation lives in ``pre2/bridge``.
"""
from __future__ import annotations

from pre2.islands import oracle_link
from pre2.recovered.renderer import blit_sprite

__all__ = ["VISIBLE_COLS", "VISIBLE_ROWS", "draw_object_sprite"]

VISIBLE_COLS = 0x14   # camera window width in tiles (cull bound)
VISIBLE_ROWS = 0x0C   # camera window height in tiles (cull bound)
_RING_ROWS = 12       # screen row = (obj_row % 12) ...
_RING_COLS = 20       # screen col = (obj_col % 20) ...


@oracle_link("1030:6544",
             "cull one object sprite vs the camera window, compute its screen offset, "
             "and blit it; [0x6BB9]=1 if drawn; CF set if culled; regs preserved",
             "RECOVERED", merge_target="frame renderer")
def draw_object_sprite(planes, obj_pos, camera_x, camera_y, mode, sprite_index,
                       blit_type, bg_off, mask_region):
    """Recover ``1030:6544`` — draw one object's sprite (or cull it).

    Returns ``True`` if drawn, ``False`` if culled (the ASM's CF=clear/set). Mutates
    ``planes`` only when drawn.

    * ``obj_pos`` — object tile position word: ``row = obj_pos >> 8``, ``col = obj_pos & 0xFF``.
    * ``camera_x``/``camera_y`` — ``[0x2DE0]``/``[0x2DE2]`` (the cull origin).
    * ``mode`` — ``cs:[1]``: ``>= 3`` uses gameplay stride 0x28 / shift 1, else 0x50 / 2.
    * ``sprite_index`` — the cache slot to blit (the blit dispatches on ``blit_type[idx]``).
    * ``blit_type`` / ``mask_region`` / ``bg_off`` — the blit's inputs (see the blit).
    """
    row = (obj_pos >> 8) & 0xFF
    col = obj_pos & 0xFF
    if (col - camera_x) & 0xFF >= VISIBLE_COLS:     # [asm 654F-6556] cull X
        return False
    if (row - camera_y) & 0xFF >= VISIBLE_ROWS:     # [asm 6558-655F] cull Y
        return False

    stride, shift = (0x28, 1) if mode >= 3 else (0x50, 2)  # [asm 6562-6573]
    y_off = ((stride * (row % _RING_ROWS)) << 4) & 0xFFFF  # [asm 6575-658B]
    x_off = (col % _RING_COLS) << shift                    # [asm 658C-659A]
    di = (y_off + x_off) & 0xFFFF                           # [asm 659D]

    typ = blit_type[sprite_index]
    mask = mask_region[(typ - 2) * 0x20:(typ - 2) * 0x20 + 0x20] if typ >= 2 else b""
    blit_sprite(planes, sprite_index, di, typ, bg_off, mask)  # [asm 65A0: call 3B58 -> blit]
    return True
