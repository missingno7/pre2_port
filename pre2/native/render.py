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

from pre2.bridge.foreground_tiles import read_foreground_state
from pre2.bridge.game_visual_state import capture_game_visual_state, render_game_visual_state
from pre2.bridge.gameplay_effects import capture_gameplay_effects
from pre2.bridge.particles import read_particles

_DS = 0x1A0F << 4
_RING_COLS, _RING_ROWS = 0x14, 0x0C    # the tile-ring moduli (see bridge/frame.py)


def native_load_dac_palette(state, dos, table_off: int, count: int = 0x10) -> None:
    """[asm 0bdc] The game's palette-set primitive: load ``count`` 6-bit DAC triples from DGROUP ``table_off``
    into DAC colours 0..count-1 (``int 10h ax=0x1012, bx=0, cx=count`` from ``DS:table_off``). The runner owns the
    DAC, so it reproduces the load. Used by the per-level palette (0ba0) and the OLDIES screen (0xb92 -> the
    green/yellow table at 0x287e). Values are 6-bit; the VGA DAC expands them (``_dac8``)."""
    d = state.data
    if len(dos.vga_palette) < 256:                                  # ensure a full DAC (snapshots carry 256)
        dos.vga_palette = list(dos.vga_palette) + [(0, 0, 0)] * (256 - len(dos.vga_palette))
    for i in range(count):
        b0 = _DS + ((table_off + i * 3) & 0xFFFF)
        dos.vga_palette[i] = (_dac8(d[b0]), _dac8(d[b0 + 1]), _dac8(d[b0 + 2]))


def native_load_level_palette(state, dos) -> None:
    """[asm 0ba0] Apply the per-level 16-colour VGA palette the standalone runner owns.

    PRE2 is a 16-colour planar game; each level has its own 16-entry palette. ``[0x2d8a]`` (the level) indexes the
    pointer table ``[0x2d00+level*2]``, whose 16 RGB triples (6-bit DAC) load into DAC colours 0..15 — exactly
    what ``0ba0`` does via int 10h ax=0x1012/cx=0x10 (``native_load_dac_palette``). native_level_init skips 0ba0 as
    'render' (it touches no DGROUP, only the DAC), so without this a different ``--level`` shows the bootstrap
    snapshot's palette. The per-level palettes are global in DGROUP, so this just selects the right one."""
    d = state.data
    level = d[_DS + 0x2D8A]
    table_off = d[_DS + 0x2D00 + level * 2] | (d[_DS + 0x2D00 + level * 2 + 1] << 8)
    native_load_dac_palette(state, dos, table_off, 0x10)


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

    # Advance the animated-tile remap cycle (1030:367D) — the render-cluster step the gameplay pass omits. The
    # VM steps [0x6BC2]/[0x6BD4] once per redraw, BEFORE the grid walk reads the current remap table, so without
    # this the standalone renders animated tiles (waving foliage, water, …) one frame STALE on every advance
    # frame (proven vs the pure-ASM oracle: forcing [0x6BC2] to the VM value drops the frame diff to 0). It is
    # render-state ([0x6BC2]/[0x6BD4] are excluded from the gameplay verify), so it belongs here — run exactly
    # once per displayed frame, matching the VM's per-redraw cadence.
    from pre2.recovered.animation import advance_animation
    fp = d[_DS + 0x6BC2] | (d[_DS + 0x6BC3] << 8)
    thr = d[_DS + 0x6BD4]
    active = d[_DS + 0x6BBD] != 0
    speed = d[_DS + 0x6BF6] | (d[_DS + 0x6BF7] << 8)
    fp, thr, _ = advance_animation(fp, thr, active, speed)
    d[_DS + 0x6BC2] = fp & 0xFF
    d[_DS + 0x6BC3] = (fp >> 8) & 0xFF
    d[_DS + 0x6BD4] = thr & 0xFF


def native_render(state, dos, display_page: int, *, game_root: str,
                  particle_capture=None, foreground_capture=None, force_gameplay: bool = False):
    """Render one gameplay frame from ``state`` (a NativeGameState). ``dos`` carries the VGA palette + registers;
    ``display_page`` is the on-screen page (``ega_display_start``). ``particle_capture``/``foreground_capture``
    are the mid-frame overlay captures; when not supplied (the standalone path) they are read straight from the
    game state here, since both the FOREGROUND tile layer (3721 — tiles drawn IN FRONT of the player) and the
    one-shot point PARTICLES (4b8e) are reconstructable from state native maintains (render slots, tilemap, camera,
    the particle list). Without this the standalone runner drew only the core frame + fireflies, so the front-tile
    layer was missing. Returns ``(planes, page)`` — four EGA plane buffers + the committed page.

    Mirrors the faithful renderer's commit-boundary capture (bridge/faithful_session.py), but sourced from
    native game state instead of the VM. Raises FaithfulVisualGap for non-gameplay scenes (no silent fallback)."""
    if foreground_capture is None:
        foreground_capture = read_foreground_state(state)          # [3721] the front tile layer
    if particle_capture is None:
        pf = read_particles(state)                                 # [4b8e] one-shot point particles
        particle_capture = pf if pf.particles else None
    fx = capture_gameplay_effects(state, particle_frame=particle_capture, foreground_frame=foreground_capture)
    gvs = capture_game_visual_state(state, dos, display_page, game_root=game_root, effects=fx,
                                    force_gameplay=force_gameplay)
    return render_game_visual_state(gvs)
