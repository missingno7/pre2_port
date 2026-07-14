"""The vertical+horizontal camera-follow controller — 1030:5643, composed over NativeGameState.

5643 keeps the camera tracking the player each frame: a horizontal follow (57A8 -> 3414/3435, the
already-recovered ``apply_camera_pan``) and a vertical follow (5663: a screen-Y window decides a target row
[0x6BF1]/active flag [0x6BEE], then scrolls the camera toward it via 33AD down / 3363 up). The scroll
primitives advance the camera cell ([0x2DE6]/[0x6BC4] sub-tile accumulator), bump the scroll buffer index
[0x2DEA], recompute the copy-source [0x2DBA] (calc_scroll_source) and trigger a plane/tile redraw (3588/348D).
The plane/tile work writes VRAM — the renderer's job — so it is NOT done here; only the camera-scroll *state*
(the ~16 DGROUP offsets 5643 touches) is reproduced. Verified byte-exact against the ASM 5643->5662 over the
demos (pre2/probes/probe_camera_scroll.py).

Render-coupling note: ``apply_camera_pan`` (the H primitive) also blits the EGA planes; that hits VRAM in the
shared address space, invisible to the DGROUP comparison, so calling it here is fine.
"""
from __future__ import annotations

from pre2.views.memory_adapter import readers
from pre2.views.dgroup_view import PlayerGlobals, PlayerView
from pre2.views.camera_pan import apply_camera_pan
from pre2.views.tables import ByteTable, Tables
from pre2.gaps import Pre2HybridGap
from pre2.native.state import DATA_SEG
from pre2.recovered.frame_renderer import calc_scroll_source

_DS_BASE = (DATA_SEG << 4) & 0xFFFFF
_CS_BASE = (0x1030 << 4) & 0xFFFFF
SCROLL_DONE_FLAG = 0x6771   # cs:[0x6771] — a per-frame code-segment flag 5643 clears at entry


def _s16(v: int) -> int:
    v &= 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


def _sar16(v: int, n: int) -> int:
    """16-bit arithmetic shift right (sar), result kept 16-bit."""
    return (_s16(v) >> n) & 0xFFFF


def _rb_cs(state, off: int) -> int:
    return state.data[(_CS_BASE + (off & 0xFFFF)) & 0xFFFFF]


def _wb_cs(state, off: int, val: int) -> None:
    state.data[(_CS_BASE + (off & 0xFFFF)) & 0xFFFFF] = val & 0xFF


# ---- vertical scroll primitives ---------------------------------------------------------------------------

def _v_scroll_down(state, dl: int) -> bool:
    """[asm 33AD..3413] Advance the camera DOWN by ``dl`` sub-tiles. Returns True if it scrolled (clc), False at
    the bottom limit (stc). Reproduces the camera-cell state; the 348D tile-row redraw is VRAM (skipped)."""
    g = PlayerGlobals(state)
    maxy = (g.map_rows - 0xB) & 0xFFFF                                      # [33B6-33BB] bottom camera limit
    if g.cam_row_word >= maxy:                                          # [33BE jae 340E]
        return False
    g.scroll_anim_ctr = (g.scroll_anim_ctr + 1) & 0xFF                            # [33C4]
    acc = (g.fine_scroll + (dl & 0xFF)) & 0xFF                         # [33C8] sub-tile accumulator += dl
    if acc < 0x10:                                                  # [33CC jb 340B]
        g.fine_scroll = acc
        return True
    acc -= 0x10                                                     # [33D3]
    cy = (g.cam_row_word + 1) & 0xFFFF                                  # [33D8] camera cell += 1
    g.cam_row_word = cy
    if cy >= maxy:                                                 # [33DC jb 33E7 ; else 33E2]
        acc = 0
    g.fine_scroll = acc
    dea = (g.row_ring + 1) & 0xFFFF                                 # [33E7-33F0] buffer index, wrap at 0xC
    if dea >= 0xC:
        dea = 0
    g.row_ring = dea                                                  # [33F5]
    g.scroll_copy_src = calc_scroll_source(g.col_ring, g.row_ring & 0xFF)       # [33F8-33FB -> 3588]
    return True


def _v_scroll_up(state, dl: int) -> bool:
    """[asm 3363..33AC] Advance the camera UP by ``dl`` sub-tiles (symmetric to _v_scroll_down). Returns True if
    it scrolled, False at the top ([0x2DE6]==0)."""
    g = PlayerGlobals(state)
    if g.cam_row_word == 0:                                             # [336C je 33A7]
        return False
    g.scroll_anim_ctr = (g.scroll_anim_ctr + 1) & 0xFF                            # [3373]
    raw = g.fine_scroll - (dl & 0xFF)                                  # [3377 sub; jns 33A4]
    if raw >= 0:
        g.fine_scroll = raw & 0xFF
        return True
    g.fine_scroll = (raw + 0x10) & 0xFF                         # [337D]
    g.cam_row_word = (g.cam_row_word - 1) & 0xFFFF                   # [3382] camera cell -= 1
    dea = g.row_ring - 1                                            # [3386-338C] buffer index, wrap to 0xB
    if dea < 0:
        dea = 0xB
    g.row_ring = dea & 0xFFFF                                # [338F-3391]
    g.scroll_copy_src = calc_scroll_source(g.col_ring, g.row_ring & 0xFF)       # [3394-3397 -> 3588]
    return True


# ---- horizontal follow (57A8) -----------------------------------------------------------------------------

def _h_init(state, dxs: int) -> None:
    """[asm 57F6..581D] Pick the horizontal scroll direction [0x6BED] (1=right, 2=left) from Xvel + screen X."""
    g = PlayerGlobals(state)
    pv = PlayerView(state)
    if pv.xvel != 0 and (pv.move_flag & 0x80):                             # [57FD] moving left
        right = False
    elif pv.xvel != 0:                                           # moving right
        right = True
    elif dxs >= 0xA:                                                # [5806] Xvel==0: far right half
        right = True
    else:
        right = False
    if right:                                                       # [5813] al=1
        if dxs >= 0x10:                                            # [5815 jl 581D]
            g.scroll_dir = 1
    else:                                                          # [580B] al=2
        if dxs <= 4:                                               # [580D jle 581A]
            g.scroll_dir = 2


def _h_follow(state) -> None:
    """[asm 57A8..581D] Horizontal camera follow: a 3-state machine (idle/right/left) on [0x6BED] driving
    apply_camera_pan (3414/3435)."""
    g = PlayerGlobals(state)
    pv = PlayerView(state)
    dx = (_sar16(pv.x, 4) - g.cam_col_word) & 0xFFFF              # [57AD-57B5] player screen X
    dxs = _s16(dx)
    if dx < 0x14 and g.unk_6BFE == 0 and pv.xvel == 0:            # [57B9/57BE/57C5] in-window & idle -> stop
        g.scroll_dir = 0
        return
    bed = g.scroll_dir
    if bed == 0:                                                   # [57CC je 57F6]
        _h_init(state, dxs)
    elif bed == 1:                                                 # [57D1 je 57E6] scrolling right
        if dxs <= 5 or not apply_camera_pan(state, "right"):       # [57E6 jle / 57EB call 3435]
            g.scroll_dir = 0
    else:                                                         # [57D8] scrolling left ([0x6BED]==2)
        if dxs >= 0xF or not apply_camera_pan(state, "left"):      # [57DA jge / 57DF call 3414]
            g.scroll_dir = 0


# ---- vertical follow (5663) -------------------------------------------------------------------------------

_SPEED_TABLE = 0x78C6   # DGROUP scroll-speed curve indexed by distance-from-target


def _v_speed(state, down: bool) -> int:
    """[asm 5738-5761 down / 5774-579B up] dl = the scroll speed from the [0x78C6] curve, by how far the camera
    is from its target. The down/up paths index the curve by the SIGNED-OPPOSITE distance (down: player below
    target = ``ay-cell``; up: ``cell-ay``). cs:[0x6771]!=0 forces dl=0x10 (snap)."""
    rb, _rw = readers(state)
    g = PlayerGlobals(state)
    pv = PlayerView(state)
    if _rb_cs(state, SCROLL_DONE_FLAG) != 0:
        return 0x10
    ay = (pv.y - g.fine_scroll) & 0xFFFF                         # player Y - sub-tile accumulator
    cell = ((g.cam_row_word + g.scroll_target_row) << 4) & 0xFFFF            # (cam cell + target) * 16
    bx = ((ay - cell) if down else (cell - ay)) & 0xFFFF           # [574C sub ax,dx] / [5788 sub bx,ax]
    if g.grid_dirty == 0:                                            # [5750/578A] the fixed curve
        return Tables(rb).speed_curve[bx]
    return ByteTable(rb, g.scroll_speed_curve_ptr)[bx]               # [578A] the alternate curve, by pointer


def _v_follow(state) -> None:
    """[asm 5663..57A7] Vertical camera follow: a screen-Y window picks a target row [0x6BF1] + active flag
    [0x6BEE], then scrolls toward it (33AD down / 3363 up)."""
    g = PlayerGlobals(state)
    pv = PlayerView(state)
    if _rb_cs(state, SCROLL_DONE_FLAG) == 0 and (g.level_flags & 4):   # [5668-5675] forced snap-down
        _v_scroll_down(state, 1)                                    # [5677 dl=1]
        return
    if pv.yvel == 0:                                             # [567D] Yvel==0 -> reset active flag
        g.cam_scroll_idle = 0
    dxs = _s16((_sar16(pv.y, 4) - g.cam_row_word) & 0xFFFF)       # [5689] player screen Y
    target = None
    if pv.yvel != 0:                                            # [5695] Yvel != 0
        if dxs >= 9:
            target = 3
        elif dxs <= 2:
            target = 8
    elif g.level_flags & 1:                                           # [56AE] Yvel==0, mode bit set
        if dxs >= 8:
            target = 7
        elif dxs <= 5:
            target = 6
    else:                                                         # [56B5] Yvel==0
        if dxs >= 0xA:
            target = 9
        elif dxs <= 3:
            target = 8
    if target is not None:                                         # [56D9]
        g.scroll_target_row = target
        g.cam_scroll_idle = (g.cam_scroll_idle + 1) & 0xFF
    if g.cam_scroll_idle == 0:                                            # [56E0 jne 56EA]
        return
    _v_scroll_apply(state, dxs)


def _v_scroll_apply(state, dxs: int) -> None:
    """[asm 56EA..57A2] Scroll the camera one step toward the target row [0x6BF1], or clear the active flag when
    it is reached / blocked at a level edge."""
    g = PlayerGlobals(state)
    pv = PlayerView(state)
    top = (g.level_prop_header + 0xB) & 0xFFFF
    # [56EA-5701] level-edge pre-check: if not past the bottom and the level top is above the camera -> scroll up
    if pv.y <= ((top << 4) & 0xFFFF) and _s16(g.level_prop_header) < _s16((g.cam_row_word - 1) & 0xFFFF):
        if not _v_scroll_up(state, _v_speed(state, down=False)):    # [576A]
            g.cam_scroll_idle = 0
        return
    if g.cam_scroll_idle == 0:                                            # [5703]
        return
    tgt = g.scroll_target_row                                               # [570D] target vs screen Y
    if tgt == (dxs & 0xFFFF):
        g.cam_scroll_idle = 0                                       # [5713 -> 57A2] reached
        return
    if _s16(tgt) > dxs:                                           # [5716 jg 576A] target below -> scroll up
        if not _v_scroll_up(state, _v_speed(state, down=False)):
            g.cam_scroll_idle = 0
        return
    # [5718] scroll down toward the target
    if pv.y <= ((top << 4) & 0xFFFF) and g.cam_row_word > g.level_prop_header:   # [5722-572C]
        g.cam_scroll_idle = 0
        return
    if not _v_scroll_down(state, _v_speed(state, down=True)):       # [5763]
        g.cam_scroll_idle = 0


# ---- top-level controller (5643) --------------------------------------------------------------------------

def native_camera_follow(state) -> None:
    """[asm 5643..5662] The whole per-frame camera-follow: horizontal (57A8, unless [0x8166]&2) then vertical
    (5663). Gated off entirely by [0x6BD9]!=0."""
    _wb_cs(state, SCROLL_DONE_FLAG, 0)                              # [5643]
    g = PlayerGlobals(state)
    if g.unk_6BD9 != 0:                                            # [564E jne 5662]
        return
    if not (g.level_flags & 2):                                       # [5655 jne 565F]
        _h_follow(state)                                           # [565C call 57A8]
    _v_follow(state)                                               # [565F call 5663]
