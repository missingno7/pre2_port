"""The native (VM-less) render entry — turn a NativeGameState into a displayed frame.

The recovered faithful renderer reconstructs the frame from the HIGH-LEVEL game state (camera, tiles, objects,
palette, HUD) — not from the ASM render cluster's draw list — so it runs directly over a NativeGameState: the
bridge readers only touch ``mem.data`` (which NativeGameState exposes). The only things it needs beyond the
game-state image are the VGA hardware pieces — the palette (``dos``) and the on-screen page (``ega_display_start``)
— which a standalone runtime owns instead of the emulated VGA.

This is the seam between the recovered gameplay sim (NativeGameState) and the recovered renderer: gameplay
produces the state, ``native_render`` produces the pixels. See native/loop.py for the gameplay side.
"""
from __future__ import annotations

from dos_re.dos import _dac8

from pre2.bridge.game_visual_state import capture_game_visual_state, render_game_visual_state
from pre2.bridge.gameplay_effects import capture_gameplay_effects

_DS = 0x1A0F << 4
_RING_COLS, _RING_ROWS = 0x14, 0x0C    # the tile-ring moduli (see bridge/frame.py)


def native_load_level_palette(state, dos) -> None:
    """[asm 0ba0] Apply the per-level 16-colour VGA palette the standalone runner owns.

    PRE2 is a 16-colour planar game; each level has its own 16-entry palette. ``[0x2d8a]`` (the level) indexes the
    pointer table ``[0x2d00+level*2]``, whose 16 RGB triples (6-bit DAC) load into DAC colours 0..15 — exactly
    what ``0ba0`` does via int 10h ax=0x1012/cx=0x10. native_level_init skips 0ba0 as 'render' (it touches no
    DGROUP, only the DAC), so without this a different ``--level`` shows the bootstrap snapshot's palette. The
    per-level palettes are global in DGROUP, so this just selects the right one (no asset reload needed)."""
    d = state.data
    level = d[_DS + 0x2D8A]
    base = _DS + (d[_DS + 0x2D00 + level * 2] | (d[_DS + 0x2D00 + level * 2 + 1] << 8))
    if len(dos.vga_palette) < 256:                                  # ensure a full DAC (snapshots carry 256)
        dos.vga_palette = list(dos.vga_palette) + [(0, 0, 0)] * (256 - len(dos.vga_palette))
    for i in range(0x10):                                          # [asm 0bb8] cx=0x10 colours from [asm 0bb6] bx=0
        r, g, b = d[base + i * 3], d[base + i * 3 + 1], d[base + i * 3 + 2]
        dos.vga_palette[i] = (_dac8(r), _dac8(g), _dac8(b))


def native_sync_render_state(state) -> None:
    """Maintain the render-ONLY scroll state the VM-less gameplay step leaves stale.

    The faithful renderer was built over the VM, where the ASM render cluster (35A1/3A27) re-derives the
    tile-ring indices [0x2DE8]/[0x2DEA] (= camera_x % 0x14 / camera_y % 0x0C) and the previous-camera cells
    [0x2DE0]/[0x2DE2] from the camera every frame. ``native_camera_follow`` advances the camera ([0x2DE4]/
    [0x2DE6]) but not these render mirrors, so a standalone runner must re-derive them here — otherwise the
    renderer reads a stale ring index and the tiles corrupt the moment the camera scrolls. (Render-only, so it
    lives outside ``native_gameplay_frame`` and never perturbs the byte-exact gameplay verify.)"""
    d = state.data
    cam_x = d[_DS + 0x2DE4] | (d[_DS + 0x2DE5] << 8)
    cam_y = d[_DS + 0x2DE6] | (d[_DS + 0x2DE7] << 8)
    for off, val in ((0x2DE8, cam_x % _RING_COLS), (0x2DEA, cam_y % _RING_ROWS),
                     (0x2DE0, cam_x), (0x2DE2, cam_y)):
        d[_DS + off] = val & 0xFF
        d[_DS + off + 1] = (val >> 8) & 0xFF


def native_render(state, dos, display_page: int, *, game_root: str,
                  particle_capture=None, foreground_capture=None):
    """Render one gameplay frame from ``state`` (a NativeGameState). ``dos`` carries the VGA palette + registers;
    ``display_page`` is the on-screen page (``ega_display_start``). ``particle_capture``/``foreground_capture``
    are the mid-frame particle/foreground overlay captures (None -> draw the core frame + fireflies only, which
    is correct when those overlays are empty/not yet captured). Returns ``(planes, page)`` — four EGA plane
    buffers + the committed page — exactly what the faithful presentation path consumes.

    Mirrors the faithful renderer's commit-boundary capture (bridge/faithful_session.py), but sourced from
    native game state instead of the VM. Raises FaithfulVisualGap for non-gameplay scenes (no silent fallback)."""
    fx = capture_gameplay_effects(state, particle_frame=particle_capture, foreground_frame=foreground_capture)
    gvs = capture_game_visual_state(state, dos, display_page, game_root=game_root, effects=fx)
    return render_game_visual_state(gvs)
