"""A minimal VM-less runtime step: advance the native gameplay one frame and render it.

This is the standalone seam — NativeGameState in, displayed EGA planes out, no VM. It runs the WHOLE recovered
per-frame gameplay loop (``native_gameplay_frame``, byte-exact over the demos incl. the boss death/respawn),
then renders the result (``native_render``). The only per-frame gap caught here is the death-to-menu carry path
(5063 out-of-lives / 5034 game-over / 4F65 level-end), which needs the flow-driver state machine; on it the
runtime just re-renders the current state (no silent ASM fallback — in standalone mode there is none).

The palette (``dos``) and on-screen page (``display_page``) are VGA pieces a full standalone runtime will own.
NOTE: the renderer reads display-page / smooth-scroll render state that the gameplay step doesn't maintain (it
was built over the VM), so the standalone runtime still needs a native render-state pass — tiles corrupt on
scroll without it.
"""
from __future__ import annotations

from pre2.checkpoints.common import Pre2HybridGap
from pre2.native.loop import native_gameplay_frame
from pre2.native.render import native_render, native_sync_render_state


def native_frame_step(state, dos, display_page: int, *, game_root: str):
    """Advance the recovered gameplay over ``state`` (in place) by one frame, then render it. Returns
    ``(planes, page)`` — the four EGA plane buffers + committed page, ready to present."""
    try:
        native_gameplay_frame(state)
    except Pre2HybridGap:
        pass   # the death-to-menu carry path (5063/5034/4F65) — not a silent ASM fallback; re-render the state
    native_sync_render_state(state)   # re-derive the render-only tile-ring + prev-camera mirrors from the camera
    return native_render(state, dos, display_page, game_root=game_root)
