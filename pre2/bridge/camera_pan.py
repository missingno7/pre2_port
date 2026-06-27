"""Bridge for the idle look-around camera pan (``1030:3414`` left / ``3435`` right).

The FSM ``anim13`` (idle look-around) path scrolls the viewport one column and reveals the newly-exposed tile
column. This composes the recovered :func:`~pre2.recovered.frame_renderer.calc_scroll_source` (``3588``) and
:func:`~pre2.recovered.frame_renderer.draw_tile_column` (``350C``) over the VM's tile map + EGA planes. The
left pan is verified byte-exact against the anim13 offline oracle (``artifacts/anim13_witness/``); the right pan
is the symmetric transcription (no witness yet — ASM_MATCHED).
"""
from __future__ import annotations

from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from pre2.bridge import frame as _F
from pre2.recovered.frame_renderer import RowFlags, calc_scroll_source, draw_tile_column

_DS = 0x1A0F


def _rw(mem, o: int) -> int:
    b = ((_DS << 4) + o) & 0xFFFFF
    return mem.data[b] | (mem.data[(b + 1) & 0xFFFFF] << 8)


def _rb(mem, o: int) -> int:
    return mem.data[((_DS << 4) + o) & 0xFFFFF]


def _ww(mem, o: int, v: int) -> None:
    b = ((_DS << 4) + o) & 0xFFFFF
    mem.data[b] = v & 0xFF
    mem.data[(b + 1) & 0xFFFFF] = (v >> 8) & 0xFF


def _wb(mem, o: int, v: int) -> None:
    mem.data[((_DS << 4) + o) & 0xFFFFF] = v & 0xFF


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def apply_camera_pan(mem, direction: str) -> bool:
    """Run the camera pan ``1030:3414`` (``direction='left'``) / ``3435`` (``'right'``) on ``mem`` in place:
    update the camera column state, recompute the scroll source, and blit the revealed 12-tile column into the
    EGA planes. Returns ``True`` if the pan happened (the inner edge gate passed), ``False`` if it was at the
    edge (the ASM's ``stc`` no-op path)."""
    if direction == "left":                                  # [3414]
        col_param = 0
        if _rw(mem, 0x2DE4) == 0:                            # [341F-3424] at the left map edge -> no pan
            return False
        _ww(mem, 0x2DE4, (_rw(mem, 0x2DE4) - 1) & 0xFFFF)    # [3426] dec [0x2DE4]
        ax = (_rw(mem, 0x2DE8) - 1) & 0xFFFF                 # [342A-342D] dec
        de8 = 0x13 if (ax & 0x8000) else ax                 # [342E jns / 3430] underflow -> 0x13
    elif direction == "right":                               # [3435]
        col_param = 0x13
        px = (_s16(_rw(mem, 0x4F1C)) >> 4) - 0x14            # [343E-3449] player tile X - 0x14
        clamp = _rw(mem, 0x8164) if _s16(_rw(mem, 0x8164)) >= px else 0xEC  # [344C-345A] jge
        if _rw(mem, 0x2DE4) >= clamp:                       # [345A-345E] jae -> at the right limit, no pan
            return False
        _ww(mem, 0x2DE4, (_rw(mem, 0x2DE4) + 1) & 0xFFFF)   # [3463] inc [0x2DE4]
        ax = (_rw(mem, 0x2DE8) + 1) & 0xFFFF                # [3467-346A] inc
        de8 = 0 if ax >= 0x14 else ax                       # [346B-3470] wrap at 0x14 -> 0
    else:
        raise ValueError(direction)

    _ww(mem, 0x2DE8, de8)                                    # [3472] [0x2DE8] = ax
    scroll_src = calc_scroll_source(de8, _rb(mem, 0x2DEA))   # [3475 -> 3588] [0x2DBA]
    _ww(mem, 0x2DBA, scroll_src)
    cell = ((_rb(mem, 0x2DE6) << 8) | _rb(mem, 0x2DE4)) & 0xFFFF  # [3478-347C] camera map cell

    planes = [bytearray(mem.data[EGA_APERTURE + i * EGA_PLANE_STRIDE:
                                 EGA_APERTURE + (i + 1) * EGA_PLANE_STRIDE]) for i in range(4)]
    flags = RowFlags(_rb(mem, 0x6BBD), _rb(mem, 0x2DF2), _rb(mem, 0x2DF4))
    tilemap = _F.read_tilemap(mem)
    blit_type = _F.read_blit_type_table(mem)
    mask_region = bytes(mem.data[(_DS << 4) + 0x2DF8:(_DS << 4) + 0x4DF8])

    bg_ptr, flags = draw_tile_column(planes, tilemap, cell, col_param, scroll_src, de8,
                                     blit_type, mask_region, flags)  # [3481 -> 350C]

    for i in range(4):                                       # write the revealed column back to the planes
        mem.data[EGA_APERTURE + i * EGA_PLANE_STRIDE:
                 EGA_APERTURE + (i + 1) * EGA_PLANE_STRIDE] = planes[i]
    _ww(mem, 0x2DF6, bg_ptr)
    _wb(mem, 0x6BBD, flags.plane_attr)
    _wb(mem, 0x2DF2, flags.tile_flags)
    _wb(mem, 0x2DF4, flags.tile_type)
    return True
