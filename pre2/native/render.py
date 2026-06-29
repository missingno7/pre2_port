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

from pre2.bridge.game_visual_state import capture_game_visual_state, render_game_visual_state
from pre2.bridge.gameplay_effects import capture_gameplay_effects


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
