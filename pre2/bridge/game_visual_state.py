"""Frame-boundary GameVisualState capture — the canonical verification substrate for FaithfulVisual.

The faithful mirror must reproduce the page the game DISPLAYS, from state that is internally consistent
with that page. An ad-hoc live read of :class:`RendererState` is NOT: the double-buffer + per-frame
scroll-ring advance mean a live read describes the page being BUILT (the back buffer `[0x2DD8]`), while
the displayed page is the committed FRONT — a *different* frame composed with the previous frame's
scroll state (see ``docs/pre2/camera_fidelity_bug.md``).

The fix is to capture at the **frame-commit boundary**: ``1030:6772`` (the palette-fade entry — the
LAST op in the per-frame main loop, AFTER the page flip). There the CRTC ``ega_display_start`` is the
just-committed frame and the scroll/camera state has not yet advanced (the next frame's update has not
run), so ``render_frame(state)@display_start == display_start`` (verified Δ≤5, the blink-phase residual,
on driven gameplay). Capturing here gives a snapshot whose state matches the displayed page exactly.

RULE: never mix the state for the page being BUILT with the page being DISPLAYED. The committed page is
``ega_display_start`` AT THE BOUNDARY; ``RendererState.dest_page`` is overridden to it so every leaf
targets the displayed page.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from dos_re.memory import EGA_PLANE_STRIDE
from pre2.bridge.render_state import read_renderer_state
from pre2.bridge.scene_state import derive_scene_kind
from pre2.recovered.faithful_visual import SceneKind, render_visual


@dataclass(frozen=True)
class GameVisualState:
    """A frame-boundary-consistent snapshot of everything FaithfulVisual needs to reproduce the
    DISPLAYED frame. Captured at the commit boundary (1030:6772) so the state matches the committed
    page — not the back buffer being built."""
    scene_kind: SceneKind
    renderer_state: object        # RendererState with dest_page == committed_page (the displayed page)
    committed_page: int           # ega_display_start at the boundary = the page actually on screen
    iris: object = None           # iris compose inputs when scene_kind==IRIS (else None)


def capture_game_visual_state(mem, dos, display_page: int, *, game_root) -> GameVisualState:
    """Capture the GameVisualState for the committed (displayed) page. Call ONLY at the frame-commit
    boundary (1030:6772) so the read is consistent with ``display_page``. ``display_page`` is the
    CRTC ``ega_display_start`` at that instant (the page on screen)."""
    page = display_page & 0xFFFF
    kind = derive_scene_kind(mem, dos)
    iris = None
    rs = None
    if kind in (SceneKind.GAMEPLAY, SceneKind.IRIS):
        rs = read_renderer_state(mem, dos, game_root=game_root)
        cam = rs.object_camera
        rs = replace(rs, dest_page=page,                       # target the DISPLAYED page, not [0x2DD8]
                     object_camera=(replace(cam, dest_page=page) if cam is not None else None))
        if kind == SceneKind.IRIS:
            from pre2.bridge import transition as _tr
            iris = replace(_tr.read_iris_inputs(mem), page=page)
    return GameVisualState(scene_kind=kind, renderer_state=rs, committed_page=page, iris=iris)


def render_game_visual_state(gvs: GameVisualState):
    """Render a captured :class:`GameVisualState` into fresh clean planes; returns ``(planes, page)``.
    Raises :class:`~pre2.recovered.faithful_visual.FaithfulVisualGap` for scenes whose leaf is not
    recovered yet (no silent fallback). Reuses the SAME recovered leaves the checkpoints verify."""
    planes = [bytearray(EGA_PLANE_STRIDE) for _ in range(4)]
    render_visual(gvs.scene_kind, gvs.renderer_state, planes, iris=gvs.iris)
    return planes, gvs.committed_page
