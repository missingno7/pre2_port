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
_ANIM_PTR = 0x6BC2          # [0x6BC2] animated-tile remap pointer
_ANIM_LO, _ANIM_HI = 0x6688, 0x6888   # its valid cycle range (3 tables) — set only during gameplay


def is_gameplay_frame(mem) -> bool:
    """Cheap gate: the animated-tile cycle pointer ``[0x6BC2]`` sits in its valid table range only
    during gameplay. Used so the gameplay renderer is not applied to menu/scene frames (the other
    visual modes are not yet recovered — see docs/pre2/scene_island.md). Heuristic, v1."""
    b = ((_DS << 4) + _ANIM_PTR) & 0xFFFFF
    return _ANIM_LO <= (mem.data[b] | (mem.data[b + 1] << 8)) <= _ANIM_HI


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
