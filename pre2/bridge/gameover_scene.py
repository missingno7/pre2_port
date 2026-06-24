"""Bridge: build the GAME-OVER scene as a SceneCompositor composition.

The game-over screen (a static diorama + the flying "GAME OVER" letters + characters + the HUD bar) is
driven by a non-gameplay loop that only runs the OBJECT pass + page flip (no grid/scroll, no 6772
boundary). The letters/characters are object sprites and the HUD is the recovered status bar — both are
already-recovered, checkpoint-grounded leaves. The diorama BACKGROUND is a static loaded image whose
decode/blit source is not recovered yet -> a ``MissingBackgroundGap``.

This bridge reads the VM state and produces:
  * the recovered overlays (object pass + HUD), reusing the exact same leaves render_frame uses;
  * the scene composition (gap background + overlays);
  * a diagnostic ``FixtureBackground`` capture (oracle planes) for the overlay-composition verify only.
"""
from __future__ import annotations

from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from pre2.bridge.render_state import read_renderer_state, retarget_page
from pre2.recovered.hud import draw_hud, draw_status_bar
from pre2.recovered.object_render import paint_sprite, plan_frame
from pre2.recovered.scene_compositor import (FixtureBackground, MissingBackgroundGap,
                                             RecoveredBackground, compose_scene)

_GAMEOVER_BG = "gameover_diorama"


def _object_overlay(rs):
    """The object pass overlay (the GAME OVER letters + characters) — mirrors render_frame's object loop."""
    def overlay(planes, page):
        if rs.object_camera is None:
            return
        banks = rs.object_src_banks or {}
        for draw in plan_frame(rs.object_sprites, rs.object_attrs or {}, rs.object_camera):
            bank = banks.get(draw.src_seg, b"")
            size = draw.src_bw * draw.full_rows * 6 + 64          # [asm read_source extent]
            paint_sprite(planes, draw, bank[draw.src_off:draw.src_off + size], rs.object_camera.row_stride)
    return overlay


def _hud_overlay(rs):
    """The HUD overlay (static status bar + dynamic lives/score/energy) — mirrors render_frame's HUD."""
    def overlay(planes, page):
        if rs.hud_chrome is None:
            return
        draw_status_bar(planes, page, rs.hud_chrome.bar)
        if rs.hud_state is not None:
            draw_hud(planes, rs.hud_state, rs.hud_chrome.font, page)
    return overlay


def build_gameover_overlays(mem, dos, *, game_root, page):
    """The recovered dynamic overlays for the game-over screen, targeting ``page``."""
    rs = retarget_page(read_renderer_state(mem, dos, game_root=game_root), page)
    return [_object_overlay(rs), _hud_overlay(rs)]


def build_gameover_scene(mem, dos, *, game_root, page, background=None):
    """Compose the game-over scene. Background defaults to the explicit MissingBackgroundGap; a recovered
    or fixture background may be passed in (the latter only by diagnostics)."""
    if background is None:
        background = MissingBackgroundGap(_GAMEOVER_BG)
    overlays = build_gameover_overlays(mem, dos, game_root=game_root, page=page)
    return compose_scene(background, overlays, page)


def capture_background_fixture(mem, page, *, name=_GAMEOVER_BG) -> FixtureBackground:
    """Capture the current VRAM planes as a DIAGNOSTIC fixture background (probes/tests ONLY).

    Used to verify the recovered overlays compose correctly over a known plate — NOT a shipped asset and
    NEVER used by the live viewer (which shows the gap until the image source is recovered)."""
    d = mem.data
    planes = tuple(
        bytes(d[EGA_APERTURE + p * EGA_PLANE_STRIDE: EGA_APERTURE + p * EGA_PLANE_STRIDE + 0x10000])
        for p in range(4)
    )
    return FixtureBackground(planes=planes, name=name)
