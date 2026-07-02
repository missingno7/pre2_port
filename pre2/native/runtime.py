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
from pre2.native.state import DATA_SEG


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


def native_iris_close(state, dos, display_page: int, *, game_root: str):
    """The level-end IRIS CLOSE (316F setup + the 31F4..32DD shrink loop): a circle centred on the player
    shrinks to black over the frozen level frame. A GENERATOR yielding ``(planes, page)`` per shrink step.

    316F [asm 31AC-31EE] centres the iris on the player (screen X/Y), picks the initial radius, and seeds the
    accelerating shrink counters; the loop composes one iris frame (compose_iris via the recovered 31F4 geometry
    + 32DE clear_span — native_render sees [0x2DD0]!=0 -> SceneKind.IRIS), then shrinks [0x2DD0] by [0x2DC0],
    stepping [0x2DC0] up every 0x14 frames, until the circle closes. (The 316F object-clear is skipped: it frees
    the gameplay objects for the exit-anim that follows, but native reloads the level fresh via native_level_end,
    and skipping it keeps the frozen objects visible behind the iris — matching the VM's page snapshot.)"""
    _DS = (DATA_SEG << 4) & 0xFFFFF
    d = state.data

    def rw(o):
        return d[_DS + (o & 0xFFFF)] | (d[_DS + ((o + 1) & 0xFFFF)] << 8)

    def ww(o, v):
        d[_DS + (o & 0xFFFF)] = v & 0xFF
        d[_DS + ((o + 1) & 0xFFFF)] = (v >> 8) & 0xFF

    def s16(v):
        return v - 0x10000 if v & 0x8000 else v

    x_off = (rw(0x4F1C) - (rw(0x2DE4) << 4)) & 0xFFFF                          # [asm 31AC-31B9] player screen X
    y_off = (((rw(0x4F1E) - (rw(0x2DE6) << 4)) & 0xFFFF) - 0x10 - d[_DS + 0x6BC4]) & 0xFFFF  # [asm 31BC-31D1]
    clamp = (2 * s16(y_off)) & 0xFFFF                                          # [asm 31D4-31DF] max(2*Y, 0xF0)
    if s16(clamp) < 0xF0:
        clamp = 0xF0
    ww(0x2DC8, x_off); ww(0x2DC6, y_off); ww(0x2DC4, clamp)
    ww(0x2DD0, 0xE6); ww(0x2DC0, 4); ww(0x2DC2, 0)                            # [asm 31E2-31EE] radius + counters
    while s16(rw(0x2DD0)) > 0:                                                # [asm 31F4..32DD] shrink loop
        yield native_render(state, dos, display_page, game_root=game_root)    # IRIS kind (compose_iris)
        c2 = (rw(0x2DC2) + 1) & 0xFFFF                                        # [asm 32B0-32C1] accel every 0x14
        if c2 >= 0x14:
            c2 = 0
            ww(0x2DC0, (rw(0x2DC0) + 1) & 0xFFFF)
        ww(0x2DC2, c2)
        ww(0x2DD0, (rw(0x2DD0) - rw(0x2DC0)) & 0xFFFF)                        # [asm 32D1] radius -= step
    ww(0x2DD0, 0)                                                             # iris closed -> off


def native_exit_anim(state, dos, display_page: int, *, game_root: str):
    """The animated level-end TALLY cutscene [asm 4CEA..4F53] — runs AFTER native_iris_close, on black. A
    GENERATOR yielding ``(planes, page)`` per frame (composed by build_tally_scene = black + the object pass +
    the SCORE/LEVEL-COMPLETED% panel). Beats: recenter the player to screen space + zero the camera (4CEA-4D1A);
    the player walks in to (0x3C,0xAF) (4D3E); 3 food sprites slide in from the right to X~0x9B (4D8E); then IF
    a bonus was collected ([0x6C9E]!=0) the player throws (4DF5) and the food flies UP counting the score up
    (4E3A-4F0B: each item accelerates up, and on reaching Y>=0x91 adds its value from the [0xA353]/[0xA375]
    tables to [0x6C0E] + sfx 8, refilled from the [0x6C12] queue); finally the player + food walk off (4F0E).

    The player walk/food-slide beats are visually verified vs the VM (the player walks in + animates, the food
    slides in, then walks off). The count-up beat (throw + food-fly-up + score add) is transcribed from
    4E3A-4F0B but GATED OFF (_COUNT_UP_RECOVERED below): on the one witness it over-counts (native 610 vs the
    VM's 200), so the shipped cutscene shows the honest pre-count-up score rather than a wrong rising one."""
    from pre2.bridge.tally_scene import build_tally_scene
    from pre2.recovered.player import player_advance_anim
    _DS = (DATA_SEG << 4) & 0xFFFFF
    d = state.data
    page = display_page & 0xFFFF

    def rw(o):
        return d[_DS + (o & 0xFFFF)] | (d[_DS + ((o + 1) & 0xFFFF)] << 8)

    def ww(o, v):
        d[_DS + (o & 0xFFFF)] = v & 0xFF
        d[_DS + ((o + 1) & 0xFFFF)] = (v >> 8) & 0xFF

    def wb(o, v):
        d[_DS + (o & 0xFFFF)] = v & 0xFF

    def s16(v):
        return v - 0x10000 if v & 0x8000 else v

    def frame():
        ap = rw(0x4F28)                                              # [asm 638B] step the player walk animation
        fr, nptr, bcf = player_advance_anim(ap, d[_DS + 0x4F25], rw)
        ww(0x4F20, fr); ww(0x4F28, nptr); wb(0x6BCF, bcf)
        _ww_ctr()                                                   # advance the free-running frame counter
        native_sync_render_state(state)
        planes, _ = build_tally_scene(state, dos, game_root=game_root, page=page)
        return planes, page

    def _ww_ctr():
        ww(0x6BD5, (rw(0x6BD5) + 1) & 0xFFFF)                       # [0x6BD5]++ (51F0/count-up timing read it)

    # [asm 4CEA-4D1A] recenter the player to screen space, zero the camera + velocities
    ww(0x4F1E, (rw(0x4F1E) - (rw(0x2DE6) << 4)) & 0xFFFF)
    ww(0x4F1C, (rw(0x4F1C) - (rw(0x2DE4) << 4)) & 0xFFFF)
    ww(0x2DE4, 0); ww(0x2DE6, 0); ww(0x4F25, 0); ww(0x4F22, 0)
    wb(0x6BC5, 0); ww(0x4F0E, 0xFFFF); ww(0x4F28, 0x7BA7)           # [asm 4D26-4D3B] free weapon slot, walk anim

    # [asm 4D3E-4D8E] the player walks in to (0x3C, 0xAF)
    while True:
        moved = False
        for off, tgt in ((0x4F1C, 0x3C), (0x4F1E, 0xAF)):
            diff = s16((rw(off) - tgt) & 0xFFFF)
            if abs(diff) >= 2:
                ww(off, (rw(off) - (2 if diff > 0 else -2)) & 0xFFFF)
                moved = True
        if not moved:
            break
        yield frame()

    # [asm 4D8E-4DE9] 3 food sprites slide in from the right (X=0x168) to X<=0x9B
    for base, y, sid in ((0x4F2E, 0xAF, 0x64), (0x4F40, 0x94, 0x62), (0x4F52, 0x9B, 0x68)):
        ww(base, 0x168); ww(base + 2, y); ww(base + 4, sid)
    while s16(rw(0x4F2E)) > 0x9B:
        for base in (0x4F2E, 0x4F40, 0x4F52):
            ww(base, (rw(base) - 3) & 0xFFFF)
        yield frame()

    # [asm 4DF5-4F0B] the food-throw score COUNT-UP. TRANSCRIBED but NOT YET CORRECT: on the one available
    # level-end witness (snapshot_pre2_20260702_111016) it over-counts (native score 610 vs the VM's 200) — the
    # item-value table / queue-drain logic below needs a bonus-collecting witness to frame-verify. Gated OFF so
    # the shipped cutscene shows the (verified) walk-in + food-slide + walk-off with the honest pre-count-up
    # score, rather than a wrong rising score. Flip _COUNT_UP_RECOVERED once it matches the VM.
    _COUNT_UP_RECOVERED = False
    if _COUNT_UP_RECOVERED and rw(0x6C9E) != 0:                     # [asm 4DEB] a bonus was collected -> count-up
        # [asm 4DF5-4E30] the player throws; the recoil velocity decays (6333 friction) to a stop
        ww(0x4F28, 0x7C6B); ww(0x4F22, 0x40); wb(0x4F24, 2)
        while rw(0x4F22) != 0:
            ww(0x4F1C, (rw(0x4F1C) + s16(rw(0x4F22)) // 16) & 0xFFFF)   # [asm 4E1F-4E2A] X += Xvel>>4
            v = abs(s16(rw(0x4F22))) - (0xC >> d[_DS + 0x4F24])          # [asm 6333] friction
            v = 0 if v < 0 else (-v if s16(rw(0x4F22)) < 0 else v)
            ww(0x4F22, v & 0xFFFF)
            yield frame()
        # [asm 4E32-4F0B] the food flies UP; on reaching the top each item adds its value to the score
        ww(0x4F28, 0x7CAF)
        while True:
            alive = False
            for k in range(0x14):                                  # the 20 effect render slots [0x52E8]
                si = 0x52E8 + k * 0x12
                if rw(si + 4) == 0xFFFF:
                    continue
                vel = rw(si + 0xE)
                if vel < 0x80:
                    vel = (vel + 8) & 0xFFFF; ww(si + 0xE, vel)     # [asm 4E61-4E6C] accelerate up
                ww(si + 2, (rw(si + 2) + (vel >> 4)) & 0xFFFF)      # [asm 4E6F-4E77] Y += vel>>4
                if s16(rw(si + 2)) < 0x91:
                    alive = True
                    continue
                t = ((rw(si + 4) & 0x1FFF) - 0x6E) & 0xFFFF         # [asm 4E82-4E93] value = tables[food type]
                vi = d[_DS + ((t - 0x5C8B) & 0xFFFF)]
                val = rw((2 * vi - 0x5CAD) & 0xFFFF)
                sc = (rw(0x6C0E) | (rw(0x6C10) << 16)) + val        # [asm 4E97-4E9B] score += value
                ww(0x6C0E, sc & 0xFFFF); ww(0x6C10, (sc >> 16) & 0xFFFF)
                ww(si + 4, 0xFFFF)                                  # [asm 4EA0] free the slot
                _emit_sfx(state, 8)                                # [asm 4EA5-4EA8] sfx 8
            if (rw(0x6BD5) & 7) == 0:                              # [asm 4EB0] refill from the [0x6C12] queue
                _exit_anim_refill(state)
            yield frame()
            if not alive and not any(d[_DS + 0x6C12 + i] for i in range(0x71)):
                break

    # [asm 4F0E-4F52] the player + the food walk off to the left
    ww(0x4F28, 0x7BA7)
    while s16(rw(0x4F2E)) > -0x34:
        for base in (0x4F2E, 0x4F40, 0x4F52):
            ww(base, (rw(base) - 2) & 0xFFFF)
        ww(0x4F1C, (rw(0x4F1C) + (3 if s16(rw(0x4F2E)) < 0 else 2)) & 0xFFFF)
        yield frame()


def _emit_sfx(state, idx):
    from pre2.native.audio import native_emit_sfx
    try:
        native_emit_sfx(state, idx)
    except Exception:                                              # noqa: BLE001 — audio is not gameplay
        pass


def _exit_anim_refill(state):
    """[asm 4EBA-4F04] spawn one queued food item ([0x6C12]+di != 0) into a free [0x52E8] slot at (0x9B, 0)."""
    _DS = (DATA_SEG << 4) & 0xFFFFF
    d = state.data
    di = None
    for i in range(0x71):
        if d[_DS + 0x6C12 + i]:
            di = i
            break
    if di is None:
        return
    for k in range(0x14):
        si = 0x52E8 + k * 0x12
        if (d[_DS + si + 4] | (d[_DS + si + 5] << 8)) == 0xFFFF:
            aid = (di + 0x6E) & 0xFFFF
            d[_DS + si] = 0x9B; d[_DS + si + 1] = 0                # X = 0x9B
            d[_DS + si + 2] = 0; d[_DS + si + 3] = 0               # Y = 0
            d[_DS + si + 4] = aid & 0xFF; d[_DS + si + 5] = (aid >> 8) & 0xFF
            d[_DS + si + 0xE] = 0; d[_DS + si + 0xF] = 0           # velocity = 0
            d[_DS + 0x6C12 + di] -= 1                              # [asm 4EFF] dequeue
            return


def native_tally_scene(state, dos, display_page: int, *, game_root: str):
    """The level-end TALLY screen — "SCORE nnnnnn / LEVEL COMPLETED nn%" over black (the recovered + VERIFIED
    51A3 text panel, fonts bridge-fed from ``state``). A GENERATOR yielding ``(planes, page)`` forever; the
    caller presents it for the tally hold, then advances (native_level_end -> carte -> next level).

    Scope: this shows the tally TEXT (the % is exact from compute_percent; the score is [0x6C0E]/[0x6C10]). The
    full 4CCB exit-anim CUTSCENE — the iris close (316F), the player walking in, and the food-item throw whose
    fly-up COUNTS the score up (adding each item's value to [0x6C0E], sfx 8) — is the deferred level-end island;
    here the score is the pre-count-up value and the character animation is not played."""
    from pre2.bridge.tally_panel import read_tally_panel
    from pre2.recovered.tally_panel import render_tally_panel
    page = display_page & 0xFFFF
    inp = read_tally_panel(state)                        # score + percent + bridge-fed glyph fonts
    while True:
        planes = [bytearray(0x10000) for _ in range(4)]  # black background
        render_tally_panel(planes, inp.score, inp.percent, page,
                           inp.digit_font, inp.letters, inp.pct_glyph)
        yield planes, page


def native_level_reveal(state, dos, display_page: int, *, game_root: str):
    """The level-START reveal: after a level loads, the VM snaps the palette to full over a BLACK screen and then
    reveals the drawn level with the 3054 center-out CURTAIN (verified vs the VM on the level-1 load witness
    snapshot_pre2_20260702_105416: black at f272 -> ~90% center-out at f288 -> full at f296). A GENERATOR yielding
    ``(planes, page)`` per curtain step; drive it once at each level start (cold boot + between-levels next level)
    so the level curtains in instead of appearing instantly."""
    native_sync_render_state(state)
    planes, page = native_render(state, dos, display_page, game_root=game_root, force_gameplay=True)
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
        base_planes, base_page = native_render(state, dos, display_page, game_root=game_root, force_gameplay=True)
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
                    new["planes"], new["page"] = native_render(state, dos, display_page, game_root=game_root,
                                                               force_gameplay=True)
                yield _reveal_frame(new["planes"], new["page"], phase[1])
        native_sync_render_state(state)
        yield native_render(state, dos, display_page, game_root=game_root, force_gameplay=True)   # settled arrival
        return
    except Pre2RespawnTransition:
        # the respawn fired this frame (the prefix already ran the death hit). Drive native_4f6c — a per-frame
        # generator — rendering EACH of the 60 bounce frames, then the checkpoint frame. Verified per-frame
        # byte-exact vs the ASM 509d loop: pre2/probes/probe_native_respawn_anim.py. force_gameplay: the
        # checkpoint restore sits the camera at the level origin (e.g. this demo's level-6 checkpoint at (0,0)),
        # which the camera!=0 SceneKind heuristic would wrongly read as the game-over/tally SCENE.
        last = None
        for _ in native_4f6c(state):
            native_sync_render_state(state)
            last = native_render(state, dos, display_page, game_root=game_root, force_gameplay=True)
            yield last
        # native_4f6c has restored the checkpoint. The VM finishes the respawn with the SAME transition as a
        # cave entrance (verified on demo 115310's level-6 death): the death frame fades to black (30C6) then the
        # checkpoint curtains in center-out (3054). Compose both here (native snapped straight to the checkpoint
        # before).
        if last is not None:
            base_planes, base_page = last
            for k in range(1, 10):                          # [asm 30C6] fade the death frame to black
                yield _vfade_frame(base_planes, base_page, k)
        yield from native_level_reveal(state, dos, display_page, game_root=game_root)   # [asm 3054] curtain in
        return
    except Pre2LevelEndTransition:
        # PROPAGATES to the caller — the between-levels flow (the VM's 4F65 -> BRAVO tally scene -> CARTE world
        # map -> next-level load) is the FLOW DRIVER's job (play_native drives the carte scene +
        # native_level_end); a state-only consumer calls native_level_end itself (see game_tick_demo).
        # (Must be re-raised EXPLICITLY: it subclasses Pre2HybridGap, which is swallowed below.)
        raise
    except Pre2HybridGap:
        # the death-to-menu carry path (5063/5034) — the state IS a real non-gameplay scene (game-over), so let
        # the SceneKind classifier run (force_gameplay stays False -> honest FaithfulVisualGap, no ASM fallback).
        native_sync_render_state(state)
        yield native_render(state, dos, display_page, game_root=game_root)
        return
    native_sync_render_state(state)   # re-derive the render-only tile-ring + prev-camera mirrors from the camera
    yield native_render(state, dos, display_page, game_root=game_root, force_gameplay=True)   # a normal gameplay frame
