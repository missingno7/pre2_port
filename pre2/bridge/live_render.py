"""Live faithful gameplay render — drive the recovered ``render_frame`` from live VM state.

This is the bridge that promotes the recovered gameplay renderer from an offline/snapshot/test
island to a **live** path: each frame it reads an explicit :class:`RendererState` from VM memory and
renders the visible gameplay frame into a CLEAN framebuffer via the recovered
:func:`pre2.recovered.render_frame.render_frame` (``rebuild=True``) — so the produced pixels depend
only on explicit state + named assets, never on the ASM-populated shadow VRAM.

The original VM still runs as the oracle/state-producer; this just renders the frame the recovered
way. A verify comparison against the VM's own page lives in the caller (the viewer / probe), not
here — so this layer never hides a divergence.

Scope: GAMEPLAY frames only. The other visual modes (intro/menu/map/transition/ending) are not yet
recovered — see docs/pre2/scene_island.md; rendering them faithfully is future scene-frame work.
"""
from __future__ import annotations

from dataclasses import replace

from dos_re.memory import EGA_PLANE_STRIDE
from pre2.bridge.render_state import read_renderer_state
from pre2.recovered.render_frame import render_frame

_DS = 0x1A0F
_CAM_X = 0x2DE4             # [0x2DE4] camera X (tiles)
_CAM_Y = 0x2DE6            # [0x2DE6] camera Y (tiles)


def _word(mem, off):
    b = ((_DS << 4) + off) & 0xFFFFF
    return mem.data[b] | (mem.data[b + 1] << 8)


def is_gameplay_frame(mem) -> bool:
    """Heuristic gameplay gate: a level is loaded with a NON-origin camera ``[0x2DE4]/[0x2DE6]``.
    The menu / map / intro / title scenes all sit at camera (0,0); gameplay (and the level-end tally)
    have a scrolled camera — and the tally is caught earlier by the iris check, so among 0Dh non-iris
    frames a non-zero camera means gameplay. (The earlier ``[0x6BC2]`` anim-ptr gate was too loose —
    menu frames share that range.) No clean gameplay flag exists (see scene_state.py); this is the
    best available signal. A gameplay frame exactly at camera origin (level start) would fall back to
    the VM frame for that frame — acceptable."""
    return (_word(mem, _CAM_X) | _word(mem, _CAM_Y)) != 0


def render_gameplay_planes(mem, dos, *, game_root, dest_page: int | None = None):
    """Render one live gameplay frame into four CLEAN EGA plane buffers; returns ``(planes, page)``.

    ``mem`` is the VM memory object, ``dos`` the runtime DOS (palette state), ``game_root`` sources
    the persistent HUD chrome asset. By default the frame targets the engine's own back page
    ``RendererState.dest_page`` ([0x2DD8]) — the page the engine renders the *current* state into, so
    a sample taken at the object-pass RET is phase-aligned (no 1-frame sprite offset). Pass
    ``dest_page`` (e.g. ``ega_display_start``) to target a specific page instead. Deplanarize the
    returned planes at ``page`` with the live DAC (``render_planar_rgb_from_planes``)."""
    rs = read_renderer_state(mem, dos, game_root=game_root)
    page = rs.dest_page if dest_page is None else (dest_page & 0xFFFF)
    cam = rs.object_camera
    rs = replace(rs, dest_page=page,
                 object_camera=(replace(cam, dest_page=page) if cam is not None else None))
    planes = [bytearray(EGA_PLANE_STRIDE) for _ in range(4)]
    # dac=None: the fade is a DAC-only effect already reflected in the live palette, so we render the
    # planes only and deplanarize with the live DAC — the planes themselves are fade-independent.
    render_frame(rs, planes, None, rebuild=True)
    return planes, page


def render_visual_planes(mem, dos, *, game_root, display_page=None):
    """Live FAITHFUL VISUAL dispatch: derive the scene kind and route to the recovered visual leaf.

    Returns ``(planes, page, scene_kind)`` for a faithfully-composed frame (GAMEPLAY or the end-level
    IRIS over the gameplay frame). For a scene whose leaf is not recovered yet (IMAGE/SCENE) it raises
    :class:`~pre2.recovered.faithful_visual.FaithfulVisualGap` — NO silent fallback to the ASM frame,
    so the missing visual work is named exactly. The recovered dispatcher reuses the same leaves the
    checkpoints verify — render_frame + compose_iris — no second copy.

    Pass ``display_page`` (the CRTC ``ega_display_start``) to render to + return the page the user is
    actually LOOKING AT — so the live viewer and --faithful-verify show/diff what is on screen, not the
    back buffer ``[0x2DD8]`` (they differ during a page-flip / curtain reveal). Default ``None`` keeps
    the engine back page (for the byte-exact offline proof)."""
    from dataclasses import replace as _replace
    from pre2.bridge.scene_state import derive_scene_kind
    from pre2.bridge import transition as _tr
    from pre2.recovered.faithful_visual import SceneKind, render_visual

    kind = derive_scene_kind(mem, dos)
    planes = [bytearray(EGA_PLANE_STRIDE) for _ in range(4)]
    if kind not in (SceneKind.GAMEPLAY, SceneKind.IRIS):
        render_visual(kind, None, planes)             # raises FaithfulVisualGap — no silent fallback
    rs = read_renderer_state(mem, dos, game_root=game_root)
    page = rs.dest_page if display_page is None else (display_page & 0xFFFF)
    cam = rs.object_camera
    rs = replace(rs, dest_page=page,
                 object_camera=(replace(cam, dest_page=page) if cam is not None else None))
    iris = None
    if kind == SceneKind.IRIS:
        iris = _replace(_tr.read_iris_inputs(mem), page=page)   # align the iris clear to our page
    render_visual(kind, rs, planes, iris=iris)
    return planes, page, kind
