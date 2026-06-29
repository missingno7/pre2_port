"""A minimal VM-less runtime step: advance the native gameplay one frame and render it.

This is the standalone seam — NativeGameState in, displayed EGA planes out, no VM. It runs the recovered
gameplay (``native_gameplay_frame``) as far as it is recovered, then renders the result (``native_render``).
Today the gameplay reaches the camera follow (5643); the secondary effect passes after the render cluster are
not yet wired, so the per-frame gap is caught here — the runtime renders the recovered state rather than
falling back to ASM (in standalone mode there is none). As more passes land, the frame completes and the catch
narrows. The palette (``dos``) and on-screen page (``display_page``) are the VGA pieces a full standalone
runtime will own; today they are supplied by the caller.
"""
from __future__ import annotations

from pre2.checkpoints.common import Pre2HybridGap
from pre2.native.loop import native_gameplay_frame
from pre2.native.render import native_render


def native_frame_step(state, dos, display_page: int, *, game_root: str):
    """Advance the recovered gameplay over ``state`` (in place) by one frame, then render it. Returns
    ``(planes, page)`` — the four EGA plane buffers + committed page, ready to present."""
    try:
        native_gameplay_frame(state)
    except Pre2HybridGap:
        pass   # the recovered prefix ran; the un-wired tail is not-yet-recovered, not a silent ASM fallback
    return native_render(state, dos, display_page, game_root=game_root)
