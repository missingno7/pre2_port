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
    # Also re-derive the scroll-copy SOURCE [0x2DBA] (the ring offset build_background_ring places tiles at)
    # from the freshly-derived ring indices. native_camera_follow sets it during normal gameplay, but a camera
    # JUMP that bypasses the follow — the death-respawn checkpoint (native_3af2) or a level start at a non-origin
    # camera — leaves it pointing at the OLD ring position, so the rebuild lays the correct tiles at the wrong
    # column offset (the level-6 respawn glitch: a narrow tree top rendered full-width + a garbage band).
    from pre2.recovered.frame_renderer import calc_scroll_source
    src = calc_scroll_source(d[_DS + 0x2DE8] | (d[_DS + 0x2DE9] << 8), d[_DS + 0x2DEA]) & 0xFFFF
    d[_DS + 0x2DBA] = src & 0xFF
    d[_DS + 0x2DBB] = (src >> 8) & 0xFF

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


_LIGHT_DARK_PAL = 0xACB7   # [asm 6791] the fixed "lights off" dark 16-colour palette (6-bit RGB)


def native_apply_palette_fade(state, dos) -> None:
    """[asm 6772] The per-frame gameplay DAC fade between the level palette ``[0x2D00+level]`` and the dark
    "lights off" palette ``0xACB7``, driven by the light pickups (player_interaction 876C/8790: ``[0x6C01]``=fade
    to dark, ``[0x6C02]``=fade to the level palette, ``[0x6C03]``=step, ``[0x6C04]``=LIGHT_STATE resting bit).

    native classifies 6772 as 'render' so the VM-less gameplay frame skips it — reproduce the DAC ramp here on
    ``dos.vga_palette`` (colours 0..15), else the "lights off/on" fade is invisible (native keeps the static level
    palette). No-op in the common case (no active fade and the lights on)."""
    d = state.data
    active = d[_DS + 0x6C01] | d[_DS + 0x6C02]
    if not active and d[_DS + 0x6C04] == 0:                          # [asm 6779] no fade + lights on -> level pal
        return
    level = d[_DS + 0x2D8A]
    lvl_pal = d[_DS + 0x2D00 + level * 2] | (d[_DS + 0x2D00 + level * 2 + 1] << 8)   # [asm 6787]

    def g(off, k):
        return d[_DS + ((off + k) & 0xFFFF)]

    if active:
        step = (d[_DS + 0x6C03] + 1) & 0xFF                          # [asm 677B] inc [0x6C03]
        d[_DS + 0x6C03] = step
        s_off, b_off = lvl_pal, _LIGHT_DARK_PAL                      # [asm 6787/6791]
        if d[_DS + 0x6C02]:                                          # [asm 6799-67A0] [0x6C02] -> swap src/dst
            s_off, b_off = b_off, s_off
        anim = 0
        dac = []
        for k in range(0x30):                                        # [asm 67A2-67C6] 16 colours x 3
            s = g(s_off, k); b = g(b_off, k); diff = s - b
            if abs(diff) > step:                                     # [asm 67B3 ja] ramp s toward b
                dac.append((s - step) if diff >= 0 else (s + step))
                anim += 1
            else:                                                   # [asm 67B7] within a step -> snap to b
                dac.append(b)
        if anim == 0:                                               # [asm 67C8-67D1] fade complete
            d[_DS + 0x6C01] = 0; d[_DS + 0x6C02] = 0
    else:                                                            # at rest with the lights off -> the dark pal
        dac = [g(_LIGHT_DARK_PAL, k) for k in range(0x30)]
    for c in range(0x10):
        dos.vga_palette[c] = (_dac8(dac[c * 3]), _dac8(dac[c * 3 + 1]), _dac8(dac[c * 3 + 2]))


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
    native_apply_palette_fade(state, dos)                          # [asm 6772] the light-pickup DAC fade (skipped
    #                                                                by the gameplay frame as 'render'); no-op idle
    if foreground_capture is None:
        foreground_capture = read_foreground_state(state)          # [3721] the front tile layer
    if particle_capture is None:
        pf = read_particles(state)                                 # [4b8e] one-shot point particles
        particle_capture = pf if pf.particles else None
    fx = capture_gameplay_effects(state, particle_frame=particle_capture, foreground_frame=foreground_capture)
    gvs = capture_game_visual_state(state, dos, display_page, game_root=game_root, effects=fx,
                                    force_gameplay=force_gameplay)
    return render_game_visual_state(gvs)
