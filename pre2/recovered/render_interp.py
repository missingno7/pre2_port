"""Object-aware inter-frame interpolation for the render model (pure).

The capture seam keeps the last two :class:`~pre2.recovered.render_model.GameFrameSnapshot`s;
a future enhanced renderer draws intermediate frames on its own (higher) display clock by
interpolating between them. This is that interpolation primitive — position-only (the standard
approach): lerp the camera and each world sprite's position, matched across frames by
``base_id`` (the cross-frame identity). Fixed-screen HUD sprites and all per-frame *state*
(palette / transition / animation / shake / HUD values / tiles) are taken from the newer frame.

Pure: operates only on GameFrameSnapshot dataclasses (no cpu/mem). See
docs/pre2/native_renderer_feasibility.md (the lerp PoC this generalises).
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import replace


def _lerp(a: int, b: int, t: float) -> int:
    return round(a + (b - a) * t)


def interpolate_frame(prev, cur, t: float):
    """Interpolate between two GameFrameSnapshots at ``t`` in [0, 1].

    ``t<=0`` yields the older positions, ``t>=1`` (or ``prev is None``) the newer frame
    verbatim. Camera ``x_px``/``y_px`` and each matched world sprite's ``world``/``screen``
    position are lerped; unmatched (newly-spawned) sprites keep their ``cur`` position and
    despawned ones are dropped; everything else comes from ``cur``.
    """
    if prev is None or t >= 1:
        return cur

    cam = replace(cur.camera,
                  x_px=_lerp(prev.camera.x_px, cur.camera.x_px, t),
                  y_px=_lerp(prev.camera.y_px, cur.camera.y_px, t))

    # match world sprites by base_id (duplicates paired in draw order via a per-id queue)
    prev_by_id: dict[int, deque] = defaultdict(deque)
    for s in prev.sprites:
        prev_by_id[s.base_id].append(s)

    sprites = []
    for s in cur.sprites:
        q = prev_by_id.get(s.base_id)
        if q:
            p = q.popleft()
            s = replace(s,
                        world_x=_lerp(p.world_x, s.world_x, t),
                        world_y=_lerp(p.world_y, s.world_y, t),
                        screen_x=_lerp(p.screen_x, s.screen_x, t),
                        screen_y=_lerp(p.screen_y, s.screen_y, t))
        sprites.append(s)

    return replace(cur, camera=cam, sprites=tuple(sprites))
