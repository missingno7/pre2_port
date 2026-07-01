"""A minimal VM-less runtime step: advance the native gameplay and render each displayed frame.

This is the standalone seam — NativeGameState in, displayed EGA planes out, no VM. It runs the WHOLE recovered
per-frame gameplay loop (``native_gameplay_frame``, byte-exact over the demos incl. the boss death/respawn),
then renders the result (``native_render``).

``native_frame_step`` is a GENERATOR: normally it yields exactly one frame, but a death-respawn is a multi-frame
TRANSITION (the 60-frame death-bounce + the checkpoint restore), so it yields each of those frames — the runner
animates the whole arc instead of teleporting to the checkpoint. The remaining gap is the death-to-menu carry
path (5063 out-of-lives / 5034 game-over / 4F65 level-end), which needs the flow-driver state machine; on it the
runtime just re-renders the current state (no silent ASM fallback — in standalone mode there is none).

The palette (``dos``) and on-screen page (``display_page``) are VGA pieces a full standalone runtime will own.
NOTE: the renderer reads display-page / smooth-scroll render state that the gameplay step doesn't maintain (it
was built over the VM), so ``native_sync_render_state`` re-derives the tile-ring mirrors before each render.
"""
from __future__ import annotations

from pre2.checkpoints.common import Pre2HybridGap, Pre2LevelEndTransition, Pre2RespawnTransition
from pre2.native.level_state import native_4f6c, native_level_end
from pre2.native.loop import native_gameplay_frame
from pre2.native.render import native_render, native_sync_render_state


def native_frame_step(state, dos, display_page: int, *, game_root: str):
    """Advance the recovered gameplay over ``state`` (in place) and ``yield`` each frame to display as
    ``(planes, page)`` — the four EGA plane buffers + committed page, ready to present.

    Normally exactly one frame. During the death-respawn transition it yields each death-bounce frame (the whole
    60-frame arc animates) then the checkpoint frame, so the standalone runner shows the animation instead of an
    instant respawn. Iterate it: ``for planes, page in native_frame_step(...): present(planes, page)``."""
    try:
        native_gameplay_frame(state)
    except Pre2RespawnTransition:
        # the respawn fired this frame (the prefix already ran the death hit). Drive native_4f6c — a per-frame
        # generator — rendering EACH of the 60 bounce frames, then the checkpoint frame. Verified per-frame
        # byte-exact vs the ASM 509d loop: pre2/probes/probe_native_respawn_anim.py.
        for _ in native_4f6c(state):
            native_sync_render_state(state)
            yield native_render(state, dos, display_page, game_root=game_root)
        native_sync_render_state(state)
        yield native_render(state, dos, display_page, game_root=game_root)   # the checkpoint, post-restore
        return
    except Pre2LevelEndTransition:
        # the level ended this frame -> advance to the next level (increment + load + re-init) and continue
        # gameplay there. (The VM's exit anim + DAC fade are the renderer's / flow driver's job; the gameplay
        # end state — the next level loaded + ready — is byte-exact.)
        native_level_end(state, game_root=game_root)
        native_sync_render_state(state)
        yield native_render(state, dos, display_page, game_root=game_root)   # the first frame of the next level
        return
    except Pre2HybridGap:
        pass   # the death-to-menu carry path (5063/5034) — not a silent ASM fallback; re-render the state
    native_sync_render_state(state)   # re-derive the render-only tile-ring + prev-camera mirrors from the camera
    yield native_render(state, dos, display_page, game_root=game_root)
