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

from pre2.checkpoints.common import (Pre2CaveTeleport, Pre2HybridGap, Pre2LevelEndTransition,
                                     Pre2RespawnTransition)
from pre2.native.level_state import native_4f6c
from pre2.native.loop import native_cave_teleport, native_gameplay_frame
from pre2.native.render import native_render, native_sync_render_state


_VIEW_ROWS = 0xB0          # the gameplay viewport height in rows (the HUD band below stays)
_ROW_BYTES = 0x28


def _vfade_frame(base_planes, page, k):
    """One frame of the 30C6 vertical fade-out over the last committed frame: rows black from the top and
    bottom converging at row 88 in 10-row bands (k=9 = fully black viewport, HUD kept). Composes the same
    geometry as the faithful ``compose_vfade_planes``."""
    top, bot = min(10 * k, 88), max(176 - 10 * k, 88)
    out = [bytearray(p) for p in base_planes]
    black = b"\x00" * _ROW_BYTES
    for r in range(_VIEW_ROWS):
        if r < top or r >= bot:
            o = (page + r * _ROW_BYTES) & 0xFFFF
            for p in range(4):
                out[p][o:o + _ROW_BYTES] = black
    return out, page


def _reveal_frame(new_planes, page, k):
    """One frame of the 3054 center-out CURTAIN reveal: the new room's viewport revealed in ``k`` symmetric
    2-byte strip-pairs (panel_copy's columns 0x14±2j) over a black viewport; the HUD band shows through."""
    from pre2.recovered.frame_renderer import panel_copy
    src = (page ^ 0x2000) & 0xFFFF                       # stage the new frame at the other page half
    out = [bytearray(p) for p in new_planes]
    view = _VIEW_ROWS * _ROW_BYTES
    for p in range(4):
        out[p][src:src + view] = new_planes[p][page:page + view]   # src <- the new room
        out[p][page:page + view] = b"\x00" * view                  # dst starts black
    panel_copy(out, src, page, k)                        # reveal k center-out strip-pairs onto the display page
    return out, page


def native_level_reveal(state, dos, display_page: int, *, game_root: str):
    """The level-START reveal: after a level loads, the VM snaps the palette to full over a BLACK screen and then
    reveals the drawn level with the 3054 center-out CURTAIN (verified vs the VM on the level-1 load witness
    snapshot_pre2_20260702_105416: black at f272 -> ~90% center-out at f288 -> full at f296). A GENERATOR yielding
    ``(planes, page)`` per curtain step; drive it once at each level start (cold boot + between-levels next level)
    so the level curtains in instead of appearing instantly."""
    native_sync_render_state(state)
    planes, page = native_render(state, dos, display_page, game_root=game_root)
    for k in range(1, 11):                                # [asm 3054] 10 center-out strip-pairs, vsync-paced
        yield _reveal_frame(planes, page, k)
    yield planes, page                                   # the fully-revealed level


def native_frame_step(state, dos, display_page: int, *, game_root: str):
    """Advance the recovered gameplay over ``state`` (in place) and ``yield`` each frame to display as
    ``(planes, page)`` — the four EGA plane buffers + committed page, ready to present.

    Normally exactly one frame. During the death-respawn transition it yields each death-bounce frame (the whole
    60-frame arc animates) then the checkpoint frame, so the standalone runner shows the animation instead of an
    instant respawn. Iterate it: ``for planes, page in native_frame_step(...): present(planes, page)``."""
    try:
        native_gameplay_frame(state)
    except Pre2CaveTeleport as tp:
        # the cave/teleport transition fired mid-frame: fade-out curtain over the CURRENT (old-area) frame,
        # black while the camera pans behind it, then the center-out reveal of the new room. The generator owns
        # ALL the state work (incl. the 53D7 mini-pass + the frame's remainder); we only compose the visuals.
        native_sync_render_state(state)
        base_planes, base_page = native_render(state, dos, display_page, game_root=game_root)
        new = {}
        pan_n = 0
        for phase in native_cave_teleport(state, tp.si):
            if phase[0] == "fade":
                yield _vfade_frame(base_planes, base_page, phase[1])
            elif phase[0] == "pan":
                pan_n += 1
                if pan_n % 2 == 0:                    # black anyway — present every 2nd step (the VM pans at
                    yield _vfade_frame(base_planes, base_page, 9)   # 70Hz, gameplay presents at ~24)
            else:                                     # ("reveal", k)
                if "planes" not in new:
                    native_sync_render_state(state)   # the camera is at the destination now
                    new["planes"], new["page"] = native_render(state, dos, display_page, game_root=game_root)
                yield _reveal_frame(new["planes"], new["page"], phase[1])
        native_sync_render_state(state)
        yield native_render(state, dos, display_page, game_root=game_root)   # the settled arrival frame
        return
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
        # PROPAGATES to the caller — the between-levels flow (the VM's 4F65 -> BRAVO tally scene -> CARTE world
        # map -> next-level load) is the FLOW DRIVER's job (play_native drives the carte scene +
        # native_level_end); a state-only consumer calls native_level_end itself (see game_tick_demo).
        # (Must be re-raised EXPLICITLY: it subclasses Pre2HybridGap, which is swallowed below.)
        raise
    except Pre2HybridGap:
        pass   # the death-to-menu carry path (5063/5034) — not a silent ASM fallback; re-render the state
    native_sync_render_state(state)   # re-derive the render-only tile-ring + prev-camera mirrors from the camera
    yield native_render(state, dos, display_page, game_root=game_root)
