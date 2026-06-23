"""Derive the faithful visual SCENE KIND from VM state.

PRE2 has **no single global scene/mode enum** (confirmed 2026-06-23: across labeled scene snapshots —
gameplay/menu/map/tally/intro/title — no DGROUP byte enumerates them; gameplay vs the 0Dh
menu/map/tally scenes has no clean discriminator either). The game dispatches scenes by which routine
runs. So the faithful visual layer **derives** the scene kind from observable VM state:

* IRIS  — the end-level iris is running: radius ``[0x2DD0] != 0`` (clean; both tally-iris snaps).
* IMAGE — intro / title artwork: VGA linear video mode 13h/19h.
* GAMEPLAY — mode 0Dh and the gameplay heuristic holds.
* SCENE — mode 0Dh otherwise (menu / map / loading / tally / game-over).

OPEN sub-problem: the GAMEPLAY-vs-SCENE 0Dh split currently rides the (imperfect)
:func:`pre2.bridge.live_render.is_gameplay_frame` heuristic — no clean data flag exists; a robust
signal (or scene-routine-entry tracking) is future work. IMAGE/SCENE leaves are not recovered yet, so
those frames fall back to the VM. Offsets stay here (bridge); the recovered dispatcher consumes the
typed :class:`~pre2.recovered.faithful_visual.SceneKind`.
"""
from __future__ import annotations

from pre2.bridge.live_render import is_gameplay_frame
from pre2.recovered.faithful_visual import SceneKind

_DS = 0x1A0F
_IRIS_RADIUS = 0x2DD0      # [0x2DD0] end-level iris radius (0 = no iris running)


def derive_scene_kind(mem, dos) -> SceneKind:
    """Resolve the current visual mode from VM state (see module docstring)."""
    if mem.data[((_DS << 4) + _IRIS_RADIUS) & 0xFFFFF] != 0:
        return SceneKind.IRIS
    video_mode = (dos.video_mode & 0x7F) if dos is not None else 0x0D
    if video_mode in (0x13, 0x19):
        return SceneKind.IMAGE
    if is_gameplay_frame(mem):
        return SceneKind.GAMEPLAY
    return SceneKind.SCENE
