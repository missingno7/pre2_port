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

from pre2.native.vga import _dac8

from pre2.views.foreground_tiles import read_foreground_state
from pre2.views.dgroup_view import PlayerGlobals, RenderSlot
from pre2.views.game_visual_state import capture_game_visual_state, render_game_visual_state
from pre2.views.gameplay_effects import capture_gameplay_effects
from pre2.views.particles import read_particles
from pre2.views.tables import ByteTable, WordTable

_RING_COLS, _RING_ROWS = 0x14, 0x0C    # the tile-ring moduli (see bridge/frame.py)


def native_load_dac_palette(state, dos, table_off: int, count: int = 0x10) -> None:
    """[asm 0bdc] The game's palette-set primitive: load ``count`` 6-bit DAC triples from DGROUP ``table_off``
    into DAC colours 0..count-1 (``int 10h ax=0x1012, bx=0, cx=count`` from ``DS:table_off``). The runner owns the
    DAC, so it reproduces the load. Used by the per-level palette (0ba0) and the OLDIES screen (0xb92 -> the
    green/yellow table at 0x287e). Values are 6-bit; the VGA DAC expands them (``_dac8``)."""
    if len(dos.vga_palette) < 256:                                  # ensure a full DAC (snapshots carry 256)
        dos.vga_palette = list(dos.vga_palette) + [(0, 0, 0)] * (256 - len(dos.vga_palette))
    words = WordTable(state.rw, table_off)
    bytes_ = ByteTable(state.rb, table_off)
    for i in range(count):
        off = i * 3                                                 # the 6-bit RGB triple in the DGROUP palette table
        rg = words[off]                                             # r=low byte, g=high byte (adjacent)
        dos.vga_palette[i] = (_dac8(rg & 0xFF), _dac8((rg >> 8) & 0xFF), _dac8(bytes_[off + 2]))


def native_load_level_palette(state, dos) -> None:
    """[asm 0ba0] Apply the per-level 16-colour VGA palette the standalone runner owns.

    PRE2 is a 16-colour planar game; each level has its own 16-entry palette. ``[0x2d8a]`` (the level) indexes the
    pointer table ``[0x2d00+level*2]``, whose 16 RGB triples (6-bit DAC) load into DAC colours 0..15 — exactly
    what ``0ba0`` does via int 10h ax=0x1012/cx=0x10 (``native_load_dac_palette``). native_level_init skips 0ba0 as
    'render' (it touches no DGROUP, only the DAC), so without this a different ``--level`` shows the bootstrap
    snapshot's palette. The per-level palettes are global in DGROUP, so this just selects the right one."""
    from pre2.views.dgroup_view import LightFadeView
    table_off = LightFadeView(state).level_palette                    # [0x2D00 + level*2] the per-level palette pointer table
    native_load_dac_palette(state, dos, table_off, 0x10)


def native_sync_render_state(state) -> None:
    """Maintain the render-ONLY scroll state the VM-less gameplay step leaves stale.

    The faithful renderer was built over the VM, where the ASM render cluster (35A1/3A27) re-derives the
    tile-ring indices [0x2DE8]/[0x2DEA] (= camera_x % 0x14 / camera_y % 0x0C) and the previous-camera cells
    [0x2DE0]/[0x2DE2] from the camera every frame. ``native_camera_follow`` advances the camera ([0x2DE4]/
    [0x2DE6]) but not these render mirrors, so a standalone runner must re-derive them here — otherwise the
    renderer reads a stale ring index and the tiles corrupt the moment the camera scrolls. (Render-only, so it
    lives outside ``native_gameplay_frame`` and never perturbs the byte-exact gameplay verify.)"""
    g = PlayerGlobals(state)
    cam_x = g.cam_col_word
    cam_y = g.cam_row_word
    g.col_ring = cam_x % _RING_COLS                        # the render-mirror ring indices + prev-camera cells
    g.row_ring = cam_y % _RING_ROWS
    g.grid_dirty_token = cam_x    # 0x2DE0: a union with the 0x55AA grid-dirty sentinel (see the field's docstring)
    g.prev_cam_cell_y = cam_y
    # Also re-derive the scroll-copy SOURCE [0x2DBA] (the ring offset build_background_ring places tiles at)
    # from the freshly-derived ring indices. native_camera_follow sets it during normal gameplay, but a camera
    # JUMP that bypasses the follow — the death-respawn checkpoint (native_3af2) or a level start at a non-origin
    # camera — leaves it pointing at the OLD ring position, so the rebuild lays the correct tiles at the wrong
    # column offset (the level-6 respawn glitch: a narrow tree top rendered full-width + a garbage band).
    from pre2.recovered.frame_renderer import calc_scroll_source
    src = calc_scroll_source(g.col_ring, g.row_ring & 0xFF) & 0xFFFF
    g.scroll_copy_src = src

    # Advance the animated-tile remap cycle (1030:367D) — the render-cluster step the gameplay pass omits. The
    # VM steps [0x6BC2]/[0x6BD4] once per redraw, BEFORE the grid walk reads the current remap table, so without
    # this the standalone renders animated tiles (waving foliage, water, …) one frame STALE on every advance
    # frame (proven vs the pure-ASM oracle: forcing [0x6BC2] to the VM value drops the frame diff to 0). It is
    # render-state ([0x6BC2]/[0x6BD4] are excluded from the gameplay verify), so it belongs here — run exactly
    # once per displayed frame, matching the VM's per-redraw cadence.
    from pre2.recovered.animation import advance_animation
    fp = g.anim_remap_ptr                                          # the animated-tile remap frame pointer + threshold
    thr = g.anim_remap_thresh
    active = g.page_dirty != 0
    speed = g.friction
    fp, thr, _ = advance_animation(fp, thr, active, speed)
    g.anim_remap_ptr = fp
    g.anim_remap_thresh = thr


_LIGHT_DARK_PAL = 0xACB7   # [asm 6791] the fixed "lights off" dark 16-colour palette (6-bit RGB)


def native_apply_palette_fade(state, dos) -> None:
    """[asm 6772] The per-frame gameplay DAC fade between the level palette ``[0x2D00+level]`` and the dark
    "lights off" palette ``0xACB7``, driven by the light pickups (player_interaction 876C/8790: ``[0x6C01]``=fade
    to dark, ``[0x6C02]``=fade to the level palette, ``[0x6C03]``=step, ``[0x6C04]``=LIGHT_STATE resting bit).

    The pass is SPLIT along the state/render seam: the tick-owned half — the [0x6C03] step increment + the
    fade-complete flag clear — runs in the gameplay frame (``loop.native_light_fade_step``, [asm 0267]); this
    render half only READS that state and reproduces the DAC ramp on ``dos.vga_palette`` (colours 0..15). It
    is idempotent per render call, so the fade advances per game TICK exactly like the VM (it previously
    advanced per render call, i.e. at --fps rate, and its state was invisible to state-only verification —
    found by the safe-hooks demo 230900 at tick 382). No-op in the common case (no fade, lights on)."""
    from pre2.views.dgroup_view import LightFadeView
    fade = LightFadeView(state)
    if fade.active:
        step = fade.step                                             # [asm 677B]'s inc already ran in the tick
        #                                                              (native_light_fade_step — the state half)
        src, dst = fade.level_palette, _LIGHT_DARK_PAL               # [asm 6787/6791]
        if fade.to_light:                                            # [asm 6799-67A0] fading BACK -> swap
            src, dst = dst, src
        dac = []
        for k in range(0x30):                                        # [asm 67A2-67C6] 16 colours x 3
            s = fade.palette_byte(src, k)
            b = fade.palette_byte(dst, k)
            diff = s - b
            if abs(diff) > step:                                     # [asm 67B3 ja] ramp s toward b
                dac.append((s - step) if diff >= 0 else (s + step))
            else:                                                   # [asm 67B7] within a step -> snap to b
                dac.append(b)
    elif fade.lights_off:                                            # at rest with the lights off -> the dark pal
        dac = [fade.palette_byte(_LIGHT_DARK_PAL, k) for k in range(0x30)]
    else:                                                            # at rest with the lights ON -> the level pal.
        # NOT an early return: the DAC colours 0..15 must be RESTORED to the level palette here. If a lights-off
        # period ends WITHOUT a fade-back — a death/respawn resets [0x6C04]=0 with no [0x6C02] fade ([asm 5237]
        # re-init) — the DAC was last left on the dark palette and nothing else rewrites it, so the screen stays
        # dark after respawn (user: "die with the lights off and it doesn't undo the light-off"). The VM's respawn
        # reloads the level DAC; mirror it every at-rest frame — idempotent with native_load_level_palette (same
        # [0x2D00+level] source), so lights-never-used levels are unchanged.
        dac = [fade.palette_byte(fade.level_palette, k) for k in range(0x30)]
    for c in range(0x10):
        dos.vga_palette[c] = (_dac8(dac[c * 3]), _dac8(dac[c * 3 + 1]), _dac8(dac[c * 3 + 2]))


def native_render(state, dos, display_page: int, *, game_root: str,
                  particle_capture=None, foreground_capture=None, force_gameplay: bool = False,
                  skip_raster: bool = False):
    """Render one gameplay frame from ``state`` (a NativeGameState). ``dos`` carries the VGA palette + registers;
    ``display_page`` is the on-screen page (``ega_display_start``). ``particle_capture``/``foreground_capture``
    are the mid-frame overlay captures; when not supplied (the standalone path) they are read straight from the
    game state here, since both the FOREGROUND tile layer (3721 — tiles drawn IN FRONT of the player) and the
    one-shot point PARTICLES (4b8e) are reconstructable from state native maintains (render slots, tilemap, camera,
    the particle list). Without this the standalone runner drew only the core frame + fireflies, so the front-tile
    layer was missing. Returns ``(planes, page)`` — four EGA plane buffers + the committed page.

    Composes the frame from the recovered render leaves at the commit boundary, sourced from native game state
    (never a VM). Raises FaithfulVisualGap for non-gameplay scenes (no silent fallback)."""
    native_apply_palette_fade(state, dos)                          # [asm 6772] the light-pickup DAC fade (skipped
    #                                                                by the gameplay frame as 'render'); no-op idle
    if foreground_capture is None and not skip_raster:            # (only the raster consumes the foreground layer)
        foreground_capture = read_foreground_state(state)          # [3721] the front tile layer
        # read_foreground_state reads the BACK page [0x2DD8] as the blit target (the VM's 3721 draws to the page
        # being composed). But native renders the CORE frame to the DISPLAY page [0x2DD6] and never flips the two
        # buffers, so [0x2DD8] is the OTHER (off-screen) buffer — the foreground tiles blit there and never show
        # (user: "foreground tiles are not in foreground"). Retarget to the same page as the core frame.
        foreground_capture.page = display_page & 0xFFFF
    if particle_capture is None:
        # prefer the pre-consume snapshot native_gameplay_frame stashed at the 4B8E entry (the one-shot [0x7DE6]
        # particles — spider-threads/sparkles — are KILLED by the consume, so the live read here would be empty);
        # fall back to a live read for callers that render without a preceding gameplay frame.
        pf = getattr(state, "particle_capture", None)
        state.particle_capture = None                              # one-shot handoff — don't reuse a stale frame
        if pf is None:
            pf = read_particles(state)                             # [4b8e] one-shot point particles (live)
        particle_capture = pf if pf.particles else None
    # non-destructive stash for SAME-tick consumers AFTER this render (the interpolation extractor runs post-
    # native_frame_step, by which time the one-shot above is already consumed). Overwritten every render —
    # None on particle-less ticks — so it can never go stale across ticks.
    state.particle_capture_last = particle_capture
    # The one-frame OPAQUE flash flag (id bit14 = [+5]&0x40) on the slots native_object_render_state captured
    # before 26FA cleared it — stashed for the SAME-tick extractor.
    flash = getattr(state, "flash_slots", None)
    state.flash_slots = None                                       # one-shot, like particle_capture
    state.flash_slots_last = flash                                 # non-destructive stash (same-tick extractor)
    if skip_raster:
        # The enhanced/interpolation path rebuilds the whole frame itself (its own background + compositor); it
        # needs only the effect STASHES above, not the ~7ms faithful raster. Skip it (the single biggest per-tick
        # cost when interpolation/widescreen is on). Returns no planes — the caller uses the enhanced compose.
        return None, display_page & 0xFFFF
    fx = capture_gameplay_effects(state, particle_frame=particle_capture, foreground_frame=foreground_capture)
    # Re-apply the flash flag so the hit/death flash draws as the VM's solid-white silhouette; the renderer state
    # is a SNAPSHOT (capture_game_visual_state reads it synchronously), so restore state.data right after —
    # leaving the flag set would desync the next frame's carried-forward state from the VM's cleared record.
    saved = None
    if flash:
        saved = [(off, RenderSlot(state, off).flags) for off in flash]   # the flags byte (opaque-flash bit)
        for off in flash:
            slot = RenderSlot(state, off)
            slot.flags = slot.flags | 0x40
    try:
        gvs = capture_game_visual_state(state, dos, display_page, game_root=game_root, effects=fx,
                                        force_gameplay=force_gameplay)
    finally:
        if saved is not None:
            for off, v in saved:
                RenderSlot(state, off).flags = v
    return render_game_visual_state(gvs)
