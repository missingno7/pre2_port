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

from pre2.gaps import (Pre2CaveTeleport, Pre2CheatCredits, Pre2GameComplete, Pre2HybridGap,
                                     Pre2LevelEndTransition, Pre2GameOverTransition, Pre2RespawnTransition)
from pre2.native.level_state import native_4f6c, native_5063
from pre2.native.loop import native_cave_teleport, native_gameplay_frame
from pre2.native.render import native_render, native_sync_render_state
from pre2.views.dgroup_view import EffectParticle, IrisView, PlayerGlobals, PlayerView, RenderSlot
from pre2.views.tables import ByteTable, WordTable

#: sentinel for native_frame_step_tagged's ``store`` default -- distinct from ``None``, which stays the
#: EXPLICIT "run the tick on the byte image" opt-out (the verify scripts' reference arm depends on that
#: meaning). Resolved lazily by _default_store() so importing this module costs nothing extra.
_DEFAULT_STORE = object()
_DEFAULT_STORE_CACHE = []


def _default_store():
    """The product's default gameplay state of record: the offset-free object graph (Stage 2.5 boot-flip).
    Cached — the controller is stateless (it re-seeds from the live image every frame), so one instance
    serves every caller and every frame.

    ``readonly_image=False`` **for the product, deliberately.** ``readonly_image=True`` asserts "the object
    graph is the COMPLETE store" and raises on any un-routed mutable write — that is a VERIFICATION invariant
    (it is how verify_object_finish proves completeness), and the verify scripts keep it by constructing
    ``ObjectStore()`` themselves, whose default stays True. Making it the PRODUCT default was a mistake in the
    boot-flip: a player hit it immediately (``gameplay tick wrote to the read-only loaded data at 0x006D``),
    because decode_input's demo-record tail writes at a runtime cursor over 0x003F..0x083C and the routing had
    two holes there (558 bytes) that no recorded demo ran long enough to reach.

    The holes are now routed, but the posture is what matters: an un-routed write is a MODELLING gap, not
    unrecovered behaviour. With ``False`` it lands in the residue image and is read back from there
    consistently — lossless, correct, and the game keeps playing; only the byte's physical home differs. That
    is not the "silent fallback" the project bans (secretly executing original ASM); it is ObjectGraphStore's
    documented residue path. Crashing a player to enforce an architectural aspiration is the wrong trade —
    verification, not the product, is where completeness gets proven."""
    if not _DEFAULT_STORE_CACHE:
        from pre2.native.object_runtime import ObjectStore
        _DEFAULT_STORE_CACHE.append(ObjectStore(readonly_image=False))
    return _DEFAULT_STORE_CACHE[0]


_VIEW_ROWS = 0xB0          # the gameplay viewport height in rows (the HUD band below stays)
_ROW_BYTES = 0x28
_FOOD_INDEX_TABLE = 0x5C8B  # food-type id -> the food-score table index [native_exit_anim 4E82-4E93]
_FOOD_SCORE_TABLE = 0x5CAD  # index -> score value (== combat_interaction.SCORE_TABLE, the same table)
_CAVE_BLACK_FRAMES = 6     # cave-teleport: how many all-black frames to show while the camera pans behind the
#                            curtain. The pan is dozens of steps for a far cave; presenting one per step made the
#                            black last ~2s. It carries no visual info (fully black), so cap it to a brief blink.


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


def _paint_player_over_iris(state, planes, page: int) -> None:
    """Draw the player sprite (slot 0x4F1C) ON TOP of a composed iris frame, so it stays visible as the iris
    fades everything else to black (the level-end effect: the world irises away but the player holds on top).
    compose_iris blacks everything outside the shrinking circle — including the player once the circle passes it —
    so re-paint the player after, targeting the just-composed display ``page``."""
    from dataclasses import replace
    from pre2.views import object_render as _obj
    from pre2.recovered.object_render import paint_sprite, plan_sprite
    spr = _obj.read_sprite(state, 0x4F1C)                     # the player render record
    if spr.sprite_id == 0xFFFF:
        return
    cam = replace(_obj.read_camera(state, frame_pre_inc=False), dest_page=page & 0xFFFF)
    draw = plan_sprite(spr, _obj.read_attr(state, spr.sprite_id), cam)
    if draw is None:
        return
    src = _obj.read_source(state, draw.src_seg, draw.src_off, draw.src_bw * draw.full_rows * 6 + 64)
    paint_sprite(planes, draw, src, cam.row_stride)


def native_iris_close(state, dos, display_page: int, *, game_root: str):
    """The level-end IRIS CLOSE (316F setup + the 31F4..32DD shrink loop): a circle centred on the player
    shrinks to black over the frozen level frame. A GENERATOR yielding ``(planes, page)`` per shrink step.

    316F [asm 31AC-31EE] centres the iris on the player (screen X/Y), picks the initial radius, and seeds the
    accelerating shrink counters; the loop composes one iris frame (compose_iris via the recovered 31F4 geometry
    + 32DE clear_span — native_render sees [0x2DD0]!=0 -> SceneKind.IRIS), then shrinks [0x2DD0] by [0x2DC0],
    stepping [0x2DC0] up every 0x14 frames, until the circle closes. (The 316F object-clear is skipped: it frees
    the gameplay objects for the exit-anim that follows, but native reloads the level fresh via native_level_end,
    and skipping it keeps the frozen objects visible behind the iris — matching the VM's page snapshot.)"""
    g, pv = PlayerGlobals(state), PlayerView(state)

    def s16(v):
        return v - 0x10000 if v & 0x8000 else v

    iris = IrisView(state)
    x_off = (pv.x - (g.cam_col_word << 4)) & 0xFFFF                            # [asm 31AC-31B9] player screen X
    y_off = (((pv.y - (g.cam_row_word << 4)) & 0xFFFF) - 0x10 - g.fine_scroll) & 0xFFFF  # [asm 31BC-31D1]
    clamp = (2 * s16(y_off)) & 0xFFFF                                          # [asm 31D4-31DF] max(2*Y, 0xF0)
    if s16(clamp) < 0xF0:
        clamp = 0xF0
    iris.center_x = x_off; iris.center_y = y_off; iris.clamp = clamp
    iris.radius = 0xE6; iris.step = 4; iris.accel_ctr = 0          # [asm 31E2-31EE] radius + counters
    while s16(iris.radius) > 0:                                                    # [asm 31F4..32DD] shrink loop
        planes, page = native_render(state, dos, display_page, game_root=game_root)   # IRIS kind (compose_iris)
        _paint_player_over_iris(state, planes, page)                          # keep the player ON TOP of the fade
        yield planes, page
        c2 = (iris.accel_ctr + 1) & 0xFFFF                                            # [asm 32B0-32C1] accel every 0x14
        if c2 >= 0x14:
            c2 = 0
            iris.step = (iris.step + 1) & 0xFFFF
        iris.accel_ctr = c2
        iris.radius = (iris.radius - iris.step) & 0xFFFF                              # [asm 32D1] radius -= step
    iris.radius = 0                                                                # iris closed -> off


def native_exit_anim(state, dos, display_page: int, *, game_root: str, state_only: bool = False):
    """The animated level-end TALLY cutscene [asm 4CEA..4F53] — runs AFTER native_iris_close, on black. A
    GENERATOR yielding ``(planes, page)`` per frame (composed by build_tally_scene = black + the object pass +
    the SCORE/LEVEL-COMPLETED% panel). Beats: recenter the player to screen space + zero the camera (4CEA-4D1A);
    the player walks in to (0x3C,0xAF) (4D3E); 3 food sprites slide in from the right to X~0x9B (4D8E); then IF
    a bonus was collected ([0x6C9E]!=0) the player throws (4DF5) and the collected food FALLS into the pot,
    counting the score up (4E3A-4F0B: each queued item spawns at the top (Y=0), accelerates DOWN, and on reaching
    the pot line Y>=0x91 adds its value — byte [(id&0x1FFF)-0x6E-0x5C8B] indexes the word table at 2*vi-0x5CAD —
    to [0x6C0E:0x6C10] + sfx 8, one item dequeued from the [0x6C12] queue every 8 frames); finally the player +
    food walk off (4F0E).

    The count-up is byte-verified vs the VM on the level-1 exit witness (snapshot_pre2_20260702_111016): with the
    316F object-clear reproduced at the entry (below), the count adds exactly the VM's 100 (score 100 -> 200).
    Without that clear it over-counted (610) by also scanning the LEVEL's leftover [0x52E8] effect sprites."""
    from pre2.views.tally_scene import build_tally_scene
    from pre2.recovered.player import player_advance_anim
    page = display_page & 0xFFFF
    g, pv = PlayerGlobals(state), PlayerView(state)

    def s16(v):
        return v - 0x10000 if v & 0x8000 else v

    def frame():
        ap = pv.anim_ptr                                            # [asm 638B] step the player walk animation
        fr, nptr, bcf = player_advance_anim(ap, pv.facing_lo, state.rw)
        pv.sprite = fr; pv.anim_ptr = nptr; g.anim_hi = bcf
        # [asm 51F0] the pot-FLAME animation — the ASM calls 51F0 each pot-phase frame (slide-in/throw/count-up),
        # BEFORE 26FA bumps [0x6BD5]: every 4th frame ([0x6BD5]&3==0) it advances the flame sprite id [0x4F56]
        # (masked to the base id, so its collectible flag drops) through 0x68..0x6D and wraps. Guard on the flame
        # id range so it fires only while the pot is on screen (the 316F clear leaves [0x4F56]=0xFFFF during the
        # player walk-in). Without this the flame was frozen (user: "the pot should be animated and it isn't").
        if (g.frame_blink & 3) == 0:
            flame = g.projectiles[2]                                          # [0x4F56] the pot-flame render-slot
            fid = flame.sprite_id
            if 0x68 <= fid < 0x6E:
                flame.sprite = 0x68 if fid + 1 >= 0x6E else fid + 1
        _ww_ctr()                                                   # advance the free-running frame counter
        native_sync_render_state(state)                            # cheap; maintains the tile-ring indices
        #                                                            [0x2DE8]/[0x2DEA] the VM's render cluster updates
        if state_only:                                              # the tick-demo verifier drains the STATE
            return None, page                                       # mutations only (count-up) — skip the tally BLIT
        planes, _ = build_tally_scene(state, dos, game_root=game_root, page=page)
        return planes, page

    def _ww_ctr():
        # A genuine 16-bit op, not a naming gap: capstone on the decompressed image confirms 1030:2708 is
        # `inc word ptr [0x6bd5]` (the 26FA record-mutation entry), unlike every OTHER site touching this
        # address (51F0's `test byte ptr [0x6bd5],3`, PlayerGlobals.frame_blink, ScrollScriptView.tick), which
        # read/write only the low byte. The word op means a low-byte carry (0xFF->0x00) also increments the
        # adjacent byte 0x6BD6 -- currently unnamed scratch (game_layout.py's player_flag_scratch) that no
        # recovered/native code reads, so the carry is harmless today but is real canonical DGROUP state.
        g = PlayerGlobals(state)
        g.frame_stamp = (g.frame_stamp + 1) & 0xFFFF          # [0x6BD5]++ (51F0/count-up timing read it)

    # [asm 316F->318B-31AA] clear the object/effect slots for the tally. The VM does this at the iris-close
    # entry (316F), which first snapshots the frozen frame to A000 (3177-3184) so the iris still shows the level;
    # native's iris renders the live (frozen) slots instead, so the clear is deferred to here — the post-iris
    # state is identical. Without it the count-up below scans the LEVEL's leftover [0x52E8] effect sprites (still
    # at their mid-level Y, already past the Y>=0x91 collect line) and over-counts. Clears the sprite-id word
    # [slot+4]=0xFFFF for 0x73 slots from 0x4F2E (the player record @0x4F1C is before the range and survives).
    for _k in range(0x73):
        RenderSlot(state, (0x4F2E + _k * 0x12) & 0xFFFF).sprite = 0xFFFF   # the effect-slot arena: free the id

    # [asm 4CEA-4D1A] recenter the player to screen space, zero the camera + velocities
    pv.y = (pv.y - (g.cam_row_word << 4)) & 0xFFFF
    pv.x = (pv.x - (g.cam_col_word << 4)) & 0xFFFF
    g.cam_col_word = 0; g.cam_row_word = 0; pv.facing = 0; pv.xvel = 0
    g.glider = 0; pv.slot0.sprite = 0xFFFF; pv.anim_ptr = 0x7BA7    # [asm 4D26-4D3B] free weapon slot, walk anim

    # [asm 4D3E-4D8E] the player walks in to (0x3C, 0xAF) — the (x, y) render-slot words at 0x4F1C/0x4F1E
    while True:
        moved = False
        for attr, tgt in (("x", 0x3C), ("y", 0xAF)):
            cur = getattr(pv, attr)
            diff = s16((cur - tgt) & 0xFFFF)
            if abs(diff) >= 2:
                setattr(pv, attr, (cur - (2 if diff > 0 else -2)) & 0xFFFF)
                moved = True
        if not moved:
            break
        yield frame()

    # [asm 4D8E-4DE9] 3 food sprites slide in from the right (X=0x168) to X<=0x9B — the 0x4F2E food-slot arena
    for i, (y, sid) in enumerate(((0xAF, 0x64), (0x94, 0x62), (0x9B, 0x68))):
        slot = g.projectiles[i]
        slot.x = 0x168; slot.y = y; slot.sprite = sid
    while s16(g.projectiles[0].x) > 0x9B:
        for i in range(3):
            slot = g.projectiles[i]
            slot.x = (slot.x - 3) & 0xFFFF
        yield frame()

    # [asm 4DF5-4F0B] the food-throw score COUNT-UP — byte-verified vs the VM (adds exactly the VM's 100 on the
    # snapshot_pre2_20260702_111016 exit witness: score 100 -> 200) now that the 316F object-clear (above) removes
    # the level's leftover [0x52E8] effect sprites. The player throws, then each collected item ([0x6C12] queue)
    # spawns at the top and falls into the pot, adding its value.
    _COUNT_UP_RECOVERED = True
    if _COUNT_UP_RECOVERED and g.item_total != 0:                      # [asm 4DEB] a bonus was collected -> count-up
        # [asm 4DF5-4E30] the player throws; the recoil velocity decays (6333 friction) to a stop
        pv.anim_ptr = 0x7C6B; pv.xvel = 0x40; pv.motion_mode = 2
        while pv.xvel != 0:
            pv.x = (pv.x + pv.xvel // 16) & 0xFFFF                 # [asm 4E1F-4E2A] X += Xvel>>4
            v = abs(pv.xvel) - (0xC >> pv.motion_mode)            # [asm 6333] friction
            v = 0 if v < 0 else (-v if pv.xvel < 0 else v)
            pv.xvel = v & 0xFFFF
            yield frame()
        # [asm 4E32-4F0B] the food falls into the pot; each item on reaching the pot line adds its value.
        # Loop shape matches the ASM: render (top, [0x6BD5]++), the slot fall/collect scan, then the refill —
        # a refill that SPAWNS a queued item continues (jmp 4E3A); termination (4F07) only fires on a refill
        # frame that finds the queue EMPTY and no slot still falling. (My earlier break-every-frame quit the
        # instant the last item spawned — before it could fall — leaving the score uncounted.)
        pv.anim_ptr = 0x7CAF                                       # [asm 4E32] the throw/count-up anim
        while True:
            yield frame()                                          # [asm 4E3A-4E50] render (26FA bumps [0x6BD5])
            alive = False
            for k in range(0x14):                                  # [asm 4E53-4EAE] the 20 effect slots [0x52E8]
                slot = EffectParticle(state, (0x52E8 + k * 0x12) & 0xFFFF)   # falling-food effect arena
                if slot.sprite == 0xFFFF:
                    continue
                alive = True                                        # [asm 4E7A] inc bp — any non-empty slot
                vel = slot.yvel
                if vel < 0x80:
                    vel = (vel + 8) & 0xFFFF; slot.yvel = vel        # [asm 4E61-4E6C] accelerate the fall
                slot.y = (slot.y + (vel >> 4)) & 0xFFFF             # [asm 4E6F-4E77] Y += vel>>4 (into the pot)
                if s16(slot.y) < 0x91:                              # [asm 4E7B] not at the pot line yet -> keep falling
                    continue
                t = (slot.sprite_id - 0x6E) & 0xFFFF                # [asm 4E82-4E93] value = tables[food type]
                vi = ByteTable(state.rb, -_FOOD_INDEX_TABLE)[t]     # food-type id -> score-table index
                val = WordTable(state.rw, -_FOOD_SCORE_TABLE)[2 * vi]  # index -> score value
                sc = (g.score_lo | (g.score_hi << 16)) + val       # [asm 4E97-4E9B] score += value
                g.score_lo = sc & 0xFFFF; g.score_hi = (sc >> 16) & 0xFFFF
                slot.sprite = 0xFFFF                                # [asm 4EA0] free the slot
                _emit_sfx(state, 8)                                # [asm 4EA5-4EA8] sfx 8
            if (g.frame_blink & 7) == 0:                                 # [asm 4EB0] a refill frame (every 8)
                if _exit_anim_refill(state):                       # [asm 4ED5] spawned a queued item -> continue
                    continue
                if not alive:                                      # [asm 4F07] queue empty AND no slot falling -> done
                    break

    # [asm 4F0E-4F52] the player + the food walk off to the left
    pv.anim_ptr = 0x7BA7
    while s16(g.projectiles[0].x) > -0x34:
        for i in range(3):
            slot = g.projectiles[i]
            slot.x = (slot.x - 2) & 0xFFFF
        pv.x = (pv.x + (3 if s16(g.projectiles[0].x) < 0 else 2)) & 0xFFFF
        yield frame()


def _emit_sfx(state, idx):
    from pre2.native.audio import native_play_sfx, player_sfx_x
    try:
        native_play_sfx(state, idx, player_sfx_x(state))          # queue ONE sfx index, panned to the player
    except Exception:                                             # noqa: BLE001 — audio is not gameplay
        pass


def _exit_anim_refill(state) -> bool:
    """[asm 4EBA-4F04] Spawn one queued food item ([0x6C12]+di != 0) into a free [0x52E8] slot at (0x9B, 0).

    Returns True iff a queued item was FOUND (whether or not a free slot was available — the ASM retries next
    refill when the pool is full, dequeuing only after a successful spawn at 4EFF), False iff the whole queue is
    empty (the ASM's 4EC9 al>=0x71 exhausted-scan -> the 4F07 termination check). The caller uses this to
    continue vs terminate the count-up."""
    g = PlayerGlobals(state)
    di = None
    for i in range(0x71):                                         # the [0x6C12] collected-item queue (byte counts)
        if g.item_queue[i]:
            di = i
            break
    if di is None:
        return False                                              # [asm 4EC9/4F07] the queue is empty
    for k in range(0x14):
        slot = EffectParticle(state, (0x52E8 + k * 0x12) & 0xFFFF)   # falling-food effect arena
        if slot.sprite == 0xFFFF:                                  # a free slot (sprite id 0xFFFF)
            slot.x = 0x9B; slot.y = 0; slot.sprite = (di + 0x6E) & 0xFFFF; slot.yvel = 0
            g.item_queue[di] = (g.item_queue[di] - 1) & 0xFF        # [asm 4EFF] dequeue
            return True
    return True                                                   # [asm 4EEB] found an item but the pool is full


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
    """The public 2-tuple API: ``for planes, page in native_frame_step(...): present(planes, page)``.
    Wraps :func:`native_frame_step_tagged`, dropping its interpolation + transition tags."""
    for planes, page, _interp, _tx in native_frame_step_tagged(state, dos, display_page, game_root=game_root):
        yield planes, page


def native_frame_step_tagged(state, dos, display_page: int, *, game_root: str, raster_normal: bool = True,
                             store=_DEFAULT_STORE):
    """Advance the recovered gameplay over ``state`` (in place) and ``yield (planes, page, interpolatable, tx)``.

    ``store`` is the gameplay-state-of-record controller (duck-typed: ``store.seed(state)`` before the tick,
    ``store.fold(state)`` after it). **The DEFAULT is now the offset-free OBJECT GRAPH** — the Stage 2.5
    boot-flip (docs/pre2/offset_free_release_plan.md), landed 2026-07-16: the product's gameplay tick mutates
    real ``Player``/``Actor``/``Camera``/... dataclasses, and the byte image is only a render/transition buffer.
    Pass ``store=None`` to opt out and run the tick on the byte image (the pre-flip path — what the verify
    scripts' reference arm uses to prove the two are identical).

    Until this flip the default was ``None``, so however many modules were converted to named/object access, the
    deployed product still executed the byte image — the reason offset_free_release_plan.md calls the boot-flip
    "the NEXT priority" and the raw-offset ratchet "the WRONG single metric". The controller is still resolved
    lazily and duck-typed, so the bridge can inject its own; it just no longer HAS to, now that the graph's
    construction ships (pre2/native/graph_layout.py, pre2/native/object_runtime.py).

    Byte-identical either way, and structurally so: the ONLY code that runs on the object graph is the tick;
    everything after it (every render and every transition, which mix raw-image access) runs on the materialised
    image, so the seam is just ``seed`` at entry then ``fold`` the moment the tick returns or raises. See
    scripts/verify_object_playloop.py and tests/test_object_playloop.py's default-path guard.

    ``raster_normal=False`` skips the faithful raster for the NORMAL gameplay tick (yields ``planes=None``) — the
    ~7ms saved when the caller presents via the enhanced compositor, which rebuilds the frame itself and needs
    only the effect stashes native_render leaves. Transition frames always raster (their planes ARE shown).

    ``interpolatable`` is True for a real gameplay frame (a single object-motion frame — the normal tick AND
    each death-bounce frame, whose parabolic arc the enhanced presenter can lerp), False for a VRAM TRANSITION
    frame (cave-teleport fade/pan/reveal, death fade-to-black + checkpoint curtain, scene passthrough), which
    must be streamed 1:1 (there is no object motion to interpolate; lerping a wipe looks wrong).

    ``tx`` is the SMOOTH-TRANSITION descriptor for a False (transition) frame, or None: ``("fade", frac)`` (a
    vertical fade-to-black over the OLD frame, frac 0->1), ``("black",)``, or ``("reveal", frac)`` (a center-out
    curtain reveal of the NEW frame, frac 0->1). The faithful ``planes`` render the effect at 320px; the smooth
    (widescreen) presenter instead drives its own full-width fade/curtain from ``frac`` (see play_native).

    Normally exactly one frame. During the death-respawn transition it yields each death-bounce frame (the whole
    60-frame arc animates) then the checkpoint frames."""
    if store is _DEFAULT_STORE:            # the shipped default: the object graph is the state of record
        store = _default_store()
    if store is not None:
        # seed the gameplay state of record from the current image (fresh each frame — picks up whatever the
        # previous frame's render/transition left), tick on it, then fold back to the image for the render.
        store.seed(state)
    try:
        native_gameplay_frame(state)
        if store is not None:
            store.fold(state)              # tick done: objects -> image (render counters preserved), ByteBackend
    except Pre2CaveTeleport as tp:
        if store is not None:
            store.fold(state)
        # the cave/teleport transition fired mid-frame: fade-out curtain over the CURRENT (old-area) frame,
        # black while the camera pans behind it, then the center-out reveal of the new room. The generator owns
        # ALL the state work (incl. the 53D7 mini-pass + the frame's remainder); we only compose the visuals.
        native_sync_render_state(state)
        base_planes, base_page = native_render(state, dos, display_page, game_root=game_root, force_gameplay=True)
        new = {}
        pan_n = 0
        for phase in native_cave_teleport(state, tp.si):
            if phase[0] == "fade":
                # tx=("fade", frac): the smooth presenter fades its own wide old-frame; frac 0->1 = clear->black
                yield (*_vfade_frame(base_planes, base_page, phase[1]), False, ("fade", phase[1] / 9.0))
            elif phase[0] == "pan":
                # The hidden camera pan yields one step per row/column moved — a far cave is DOZENS of steps, and
                # presenting a black frame per (2) steps made the between-curtains black last ~2s. The pan is
                # fully-black anyway, so its length carries no information: present a SHORT FIXED number of black
                # frames (a brief blink) and drive the remaining pan silently to the destination.
                pan_n += 1
                if pan_n <= 2 * _CAVE_BLACK_FRAMES and pan_n % 2 == 0:
                    yield (*_vfade_frame(base_planes, base_page, 9), False, ("black",))
            else:                                     # ("reveal", k)
                if "planes" not in new:
                    native_sync_render_state(state)   # the camera is at the destination now
                    new["planes"], new["page"] = native_render(state, dos, display_page, game_root=game_root,
                                                               force_gameplay=True)
                # tx=("reveal", frac): the smooth presenter composes the wide NEW room and wipes it in
                yield (*_reveal_frame(new["planes"], new["page"], phase[1]), False, ("reveal", phase[1] / 10.0))
        native_sync_render_state(state)
        yield (*native_render(state, dos, display_page, game_root=game_root, force_gameplay=True), True, None)
        return
    except Pre2RespawnTransition:
        if store is not None:
            store.fold(state)
        # the respawn fired this frame (the prefix already ran the death hit). Drive native_4f6c — a per-frame
        # generator — rendering EACH of the 60 bounce frames, then the checkpoint frame. Verified per-frame
        # byte-exact vs the ASM 509d loop: pre2/probes/probe_native_respawn_anim.py. force_gameplay: the
        # checkpoint restore sits the camera at the level origin (e.g. this demo's level-6 checkpoint at (0,0)),
        # which the camera!=0 SceneKind heuristic would wrongly read as the game-over/tally SCENE.
        last = None
        for _ in native_4f6c(state):
            native_sync_render_state(state)
            last = native_render(state, dos, display_page, game_root=game_root, force_gameplay=True)
            yield (*last, True, None)                              # a death-bounce frame -> interpolatable
        # native_4f6c has restored the checkpoint. The VM finishes the respawn with the SAME transition as a
        # cave entrance (verified on demo 115310's level-6 death): the death frame fades to black (30C6) then the
        # checkpoint curtains in center-out (3054). Compose both here (native snapped straight to the checkpoint
        # before).
        if last is not None:
            base_planes, base_page = last
            for k in range(1, 10):                          # [asm 30C6] fade the death frame to black
                yield (*_vfade_frame(base_planes, base_page, k), False, ("fade", k / 9.0))
        for i, (_rp, _rpg) in enumerate(native_level_reveal(state, dos, display_page, game_root=game_root), 1):
            yield (_rp, _rpg, False, ("reveal", i / 11.0))  # [asm 3054] the checkpoint curtains in center-out
        return
    except Pre2LevelEndTransition:
        if store is not None:
            store.fold(state)        # fold gameplay state to the image before the caller's raw-.data load
        # PROPAGATES to the caller — the between-levels flow (the VM's 4F65 -> BRAVO tally scene -> CARTE world
        # map -> next-level load) is the FLOW DRIVER's job (play_native drives the carte scene +
        # native_level_end); a state-only consumer calls native_level_end itself (see game_tick_demo).
        # (Must be re-raised EXPLICITLY: it subclasses Pre2HybridGap, which is swallowed below.)
        raise
    except Pre2GameOverTransition:
        if store is not None:
            store.fold(state)
        # [asm 5063] death -> game-over restart. Render the death-bounce arc (native_5063 yields the 60 bounce
        # frames), then RE-RAISE: the restart re-enters main's 0x12f front-end flow (447d + carte + level-1
        # reload), which is the FLOW DRIVER's job (like level-end). native_5063 ends with the level/score reset
        # (an unloaded state), so only the bounce frames are rendered here.
        for _ in native_5063(state):
            native_sync_render_state(state)
            yield (*native_render(state, dos, display_page, game_root=game_root, force_gameplay=True), True, None)
        raise
    except Pre2GameComplete:
        if store is not None:
            store.fold(state)
        # [asm 5034] THE END — the player cleared the final level 0xE. The game-complete SCENE (THEEND.SQZ fade +
        # the 25F6 creators screen -> menu) is the FLOW DRIVER's job (play_native's the_end_restart). Re-raise
        # EXPLICITLY: it subclasses Pre2HybridGap, which the handler below swallows.
        raise
    except Pre2CheatCredits:
        if store is not None:
            store.fold(state)
        # [asm 247B->2505] the dev-credits cheat combo — an OVERLAY scene the flow driver shows then RESUMES the
        # same level. Re-raise EXPLICITLY (subclasses Pre2HybridGap, swallowed below).
        raise
    except Pre2HybridGap:
        if store is not None:
            store.fold(state)
        # a real non-gameplay scene reached via a carry path we don't drive as a transition — let the SceneKind
        # classifier run (force_gameplay stays False -> honest FaithfulVisualGap, no ASM fallback).
        native_sync_render_state(state)
        yield (*native_render(state, dos, display_page, game_root=game_root), False, None)   # scene passthrough
        return
    native_sync_render_state(state)   # re-derive the render-only tile-ring + prev-camera mirrors from the camera
    yield (*native_render(state, dos, display_page, game_root=game_root, force_gameplay=True,
                          skip_raster=not raster_normal), True, None)  # normal tick (raster skipped -> enhanced path)
