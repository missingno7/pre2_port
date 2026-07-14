"""The PRE2 death / respawn / level-state handlers — the 4C69 dispatch targets, VM-independent native source.

``native_4f6c`` is the respawn-to-checkpoint handler (``[0x6be4]==1``): it plays the death-bounce, then restores
the level to its checkpoint and continues the gameplay loop. The death (5063) / game-over (5034) / level-end
(4F65) handlers — which return carry to main's level-change at 0x12f — are the remaining gaps. See the
``pre2-level-state-machine`` memory for the full map.
"""
from __future__ import annotations

from pre2.gaps import Pre2ExpertEater, Pre2GameOverTransition, Pre2HybridGap
from pre2.native.level_init import native_3af2, native_5237, native_level_start
from pre2.native.loop import native_death_bounce_509d
from pre2.native.state import DATA_SEG
from pre2.views.dgroup_view import PlayerGlobals, PlayerView
from pre2.views.tables import ByteTable
from pre2.native.dgroup_offsets import ITEM_QUEUE, WARP_TABLE

_DS = DATA_SEG << 4
# [asm 4fe1-4fff] effect-sprite types (relative to 0x35) the checkpoint restore must NOT overwrite — the
# re-init's freshly-spawned transient sprites are kept rather than rolled back to the snapshot.
_TRANSIENT_TYPES = {0xD, 0x2C, 0x41, 0xA9, 0xAA, 0xB6, 0xE0}


def native_51df(state) -> None:
    """[asm 51DF] Respawn cleanup: zero the object-backup scratch ``[0x6c12..+0x71]`` + ``[0x6c9e]``."""
    state.data[_DS + ITEM_QUEUE:_DS + ITEM_QUEUE + 0x71] = b"\x00" * 0x71   # [asm 51e2-51e7] the item-count table
    PlayerGlobals(state).item_total = 0                                # [asm 51e9]


def native_5063(state):
    """[asm 5063] The DEATH -> GAME-OVER-RESTART handler (4C69's [0x6be5]==1). Plays the death-bounce (509d), then
    resets to level 1 with a zero score and returns carry to main's 0x12f, which RELOADS level 1 (the game
    restarts). A GENERATOR yielding the 60 bounce frames (like native_4f6c) so the flow driver renders the death
    arc; after it drains, the caller reloads level 1 (native_level_init) and the gameplay loop continues.

    The non-gameplay sub-calls are the flow driver's: 25c7 (a wait-for-key-release busy-wait, no DGROUP state),
    the 0x11 game-over song (audio), and 9b23's game-over-screen A000 blit (render). 9b23's DGROUP camera reset
    (it runs since [0x2879]==0 here) IS reproduced. The [0x2D8A]=0 + score-zero + 51df are the gameplay contract."""
    yield from native_death_bounce_509d(state)                     # [asm 5069] 509d death-bounce (60 frames)
    _game_over_reset(state)                                         # [asm 506c tail]


def _game_over_reset(state) -> None:
    """[asm 506c tail] The game-over DGROUP reset shared by ``native_5063`` (the ``[0x6be5]==1`` game-over) and
    the ``native_4f6c`` real-death branch — ``5063`` restores ``[0x2875]`` then does the bounce and FALLS INTO
    ``506c``, and ``4f6c``'s real-death path ``jmp``s to the same ``506c``. The reset: the ``9b23`` camera reset,
    the death pose, restart-at-level-1, a zero score, and the ``51df`` cleanup. (The bounce + the ``9b23/25c7``
    game-over presentation + music are the flow driver's; this is the gameplay contract only.)"""
    g, pv = PlayerGlobals(state), PlayerView(state)
    g.cam_col_word = 0; g.cam_row_word = 0; g.row_factor = 0; g.fine_scroll = 0   # [asm 9b35-9b3e] camera reset (9b23)
    pv.sprite = 0x0D                                                # [asm 5078] the death pose
    g.level = 0                                                    # [asm 507e] restart at level 1
    g.score_lo = 0; g.score_hi = 0; g.score_mid = 0                # [asm 5083-508f] score = 0
    native_51df(state)                                             # [asm 5095] cleanup


def native_4f6c(state):
    """[asm 4F6C] The respawn-to-checkpoint handler (4C69's ``[0x6be4]==1`` target). Plays the death-bounce
    (509d), then — unless a real death is pending (``[0x2879]`` or ``[0x6be5]``) — snapshots the live
    effect-sprite-source + active-flag tables, re-inits the level (5237), drops the player at the checkpoint
    ``[0x6bad]``/``[0x6baf]``, re-inits the camera (3af2), restores the snapshot (keeping the re-init's transient
    effect sprites + re-freeing the slots that were dead), and cleans up (51df). Returns with NO level change —
    the gameplay loop continues. A pending REAL death instead takes the shared 506c game-over tail: the reset
    is applied (``_game_over_reset``) and :class:`Pre2GameOverTransition` propagates to the flow driver.

    GENERATOR: ``yield``s once per death-bounce frame (60 frames) so the caller renders the whole arc, THEN runs
    the checkpoint restore (instantaneous — the screen jumps to the checkpoint on the next rendered frame). Drive
    to completion to apply the respawn (``for _ in native_4f6c(state): render(...)``).

    Verified byte-exact end-to-end vs the ASM: pre2/probes/probe_native_respawn.py drives the blocking ASM 4F6C
    through the timer/retrace machinery and diffs DGROUP at the RET 0x5033 -> 0 diffs (render/timing excluded);
    pre2/probes/probe_native_respawn_anim.py additionally diffs EACH bounce frame vs the ASM 509d loop."""
    g, pv = PlayerGlobals(state), PlayerView(state)
    yield from native_death_bounce_509d(state)                      # [asm 4f6c] 509d death-bounce (per-frame)
    if g.input_source != 0 or g.end_signal != 0:                   # [asm 4f6f-4f80] a real death is pending
        # [asm 4f76/4f80 jmp 506c] the bounce is done; this is the SAME game-over tail as native_5063 (5063 falls
        # into 506c). Apply the shared reset, then hand off to the flow driver's game-over (scene + restart) —
        # exactly the [0x6be5]==1 path. (native_frame_step's respawn handler lets this propagate; play_native's
        # game_over_restart runs the 9b23 scene + level-1 restart.)
        _game_over_reset(state)
        raise Pre2GameOverTransition()

    # [asm 4f91-4fa6] snapshot the 0x46 effect-sprite source values [0x8f1d]+4 (stride 7) -> [0x6c12] (stride 2)
    for i in range(0x46):
        g.item_snapshot[i] = g.effect_sources[i].sprite
    g.item_snapshot[0x46] = 0x55AA                                 # [asm 4fa3] end marker
    # [asm 4fa7-4fbd] snapshot the 0x50 active flags [0x8c8d]+3 (stride 5) -> [0xa2a8] (1 = live, 0 = free)
    for i in range(0x50):
        g.active_flag_snapshot[i] = 0 if g.bonus_cells[i].cell == 0xFFFF else 1

    native_5237(state)                                            # [asm 4fbf] 5237 level re-init
    pv.x = g.checkpoint_x; pv.y = g.checkpoint_y                  # [asm 4fc2-4fcb] player -> the checkpoint
    native_3af2(state)                                           # [asm 4fce] 3af2 camera-init

    # [asm 4fd1-500a] restore the effect-sprite sources, skipping slots whose re-init type is transient
    for i in range(0x46):
        ax = g.item_snapshot[i]
        src = g.effect_sources[i]
        if ((src.sprite - 0x35) & 0xFF) not in _TRANSIENT_TYPES:
            src.sprite = ax
    # [asm 500c-5022] re-free any slot the snapshot recorded as dead -> [0x8c8d]+3 = 0xffff
    for i in range(0x50):
        if g.active_flag_snapshot[i] == 0:
            g.bonus_cells[i].cell = 0xFFFF

    native_51df(state)                                           # [asm 5024] cleanup
    # [asm 5030-5033] ax=0; clc; ret — no level change, the gameplay loop continues


def level_end_takes_tally(mode: int, level: int) -> bool:
    """[asm 4C69] Whether this level-end runs the TALLY flow (4CCB exit-anim/BRAVO/carte) or the cave-style
    CURTAIN (``call 30C6`` + ``jmp 4F65`` — no tally, no carte).

    The dispatch: a normal end (``mode==1``) on a MAIN level (<0xA) falls into 4CCB = TALLY; a normal end on a
    BONUS level (>=0xA, 4CBA/4CC1) closes the curtain and reloads. A warp (``mode>1``) FROM a main level (4C8F)
    jumps INTO its bonus level curtain-style; a warp FROM a bonus level (4C7E reverse lookup) re-enters the 4CBA
    normal path = TALLY. So: tally iff ``(mode==1) == (level<0xA)``."""
    return (mode == 1) == (level < 0xA)


def native_level_end(state, *, game_root: str) -> None:
    """[asm 4C69 [0x6be6]==1 -> 4cba -> 4F65 -> main 0x12f] The LEVEL-END transition: advance to the next level.

    The VM path runs the exit anim (4ccb, a visual multi-frame scene whose player moves are overwritten by the
    load), increments ``[0x2d8a]``, calls 4F65 (5237 re-init + carry), and re-enters main at 0x12f which clears
    [0x6be6], DAC-fades, runs the level-init (447d -> 3ed6 loader) for the new level, then sets the level-change
    flags. The recovered GAMEPLAY transition keeps the byte-exact end state (the next level loaded + ready) and
    leaves the exit-anim / fade to the renderer / flow driver: increment the level, ``native_level_init`` the new
    one (load + 5237 + player-init + camera, all individually byte-exact), then apply the 0x12f flag writes.

    The next level is the 4C74 table lookup: [0x6be6]==1 normal end -> ``+1``; [0x6be6]>1 WARP -> a main level
    (<0xA) jumps to its BONUS level ``[0x2cf6+level]`` (no +1, the ASM jmp 4f65 skips 4cba), and a bonus level
    (>=0xA) reverse-looks-up its source main level in ``[0x2cf6]`` then advances ``+1`` (jmp 4cba -> 4cc4)."""
    g, pv = PlayerGlobals(state), PlayerView(state)
    warp_table = ByteTable(state.rb, WARP_TABLE)
    level = g.level
    if g.level_end_mode > 1:                                      # [asm 4C74] a WARP (not the normal +1 end)
        if level < 0xA:                                          # a main level -> its bonus level [0x2cf6+level]
            g.level = warp_table[level]                             # [asm 4c8f-4c90] (jmp 4f65: NO +1) — warp table
        else:                                                    # a bonus level -> the source main level, then +1
            # [asm 4c7e-4c85] reverse-lookup: scan [0x2cf6] for the entry whose value == this bonus level. The
            # table only maps the FIVE table-warp bonuses (0x00/0x0A/0x0B/0x0C/0x0E, reached by a `[0x6be6]=0xff`
            # id-0x137 tile on a main level); those always resolve here. The OTHER bonuses (0x0D/0x0F=LEVELG/0x10)
            # are NOT in the table and never reach this branch: they exit via the id-0x117 end-of-level tile
            # (8859->882A) which REMAPS [0x2d8a] (LEVELG 0x0F->6) and sets [0x6be6]=1, so they take the normal-end
            # path above (recovered in player_interaction.loop2_handler num==0xE2 — that is LEVELG's real exit).
            # If one ever did land here the ORIGINAL scans off the end of the 10-entry table into garbage and jumps
            # to a non-existent level (0x0F -> level 0x94) — i.e. the real game crashes too. Fail loud rather than
            # reproduce that crash.
            src = next((i for i in range(0xA) if warp_table[i] == level), None)   # [asm 4c7e-4c85]
            if src is None:
                raise Pre2HybridGap(f"native level-warp: bonus level {level:#x} not a [0x2cf6] table-warp bonus "
                                    f"(the original scans past the table into garbage here — an unreachable state; "
                                    f"LEVELG/0x0D/0x10 exit via the id-0x117 end-of-level remap, not this warp)")
            g.level = (src + 1) & 0xFF                           # [asm 4c89 index -> 4cba -> 4cc4 +1]
    else:                                                        # [asm 4cba/4cc4] the normal end -> +1
        g.level = (level + 1) & 0xFF
    # [asm 0137 or ax,ax / je 0163] main's 0x141-0155 level-start block (lives [0x27D8]=2, BONUS-letters mask
    # [0x6CA7]=0, utensils mask [0x6CA8]=0, damage [0x7B19]/[0x7B18]) runs ONLY when ax!=0 — a FRESH game start.
    # The between-levels path enters via 4F65 (`call 5237; xor ax,ax; stc; ret`), so ax=0 and the block is SKIPPED:
    # that persistent state carries into the next level (user: collected BONUS letters were resetting each level).
    # native_level_start runs the block unconditionally, so preserve those five offsets across the load.
    # [asm 4F62] cleanup — clear [0x6c9e] ITEM_TOTAL + the object-backup scratch [0x6c12..+0x71] — runs ONLY on the
    # TALLY path (4ccb -> 4F5E `inc; call 51df; call 5237`). The BONUS/WARP path (4cba/4cc1/4cc4 -> `jmp 4F65`)
    # jumps PAST 4F62, so it SKIPS the cleanup and CARRIES the collected-food count into the bonus level (verified:
    # the LEVEL7->LEVELG warp preserves [0x6c9e]=0xc/[0x6c12]/[0x6c5b] unchanged). Gate it on the same tally split.
    if level_end_takes_tally(g.level_end_mode, level):
        native_51df(state)
    else:
        # The bonus/warp path also skips the multi-frame exit-anim/reveal whose object_render erase FREES the
        # player's own sprite record [0x4F0A..0x4F1B] to 0xFFFF (the 3ED6 loader clears from 0x4F1C, NOT this
        # slot). It's render-only garbage while the sprite is suppressed ([0x4F0E]==0xFFFF at level entry), so
        # native collapses that erase to its end state here — matching the VM's LEVELG entry.
        state.data[_DS + 0x4F0A:_DS + 0x4F1C] = b"\xFF" * (0x4F1C - 0x4F0A)
    # [asm 0163] the EXPERT-EATER gate — main checks it AFTER 4F65 advanced [0x2d8a] (above) but BEFORE the loader
    # (447d, below): a BEGINNER ([0xB197]==0) who advanced into level 8/9 (the penguin is expert-only) is shown
    # CASTLE.SQZ and sent back to the menu, so the next level is NEVER loaded. Raise here (post-advance, pre-load) so
    # the flow driver drives the wall + re-enters the menu; the 3ED6 load is skipped (matches the VM — no load).
    if g.level in (8, 9) and g.mode == 0:                        # == is_expert_eater_wall (inlined to avoid a cycle)
        raise Pre2ExpertEater()
    # the level-start block resets these, but the VM carries them across a between-levels load — preserve them
    keep = (g.lives, g.bonus_letters, g.utensils_mask, g.attack_v19, g.attack_phase)   # [0x27D8/6CA7/6CA8/7B19/7B18]
    native_level_start(state, game_root=game_root)               # [asm 0x13e..0155] load + re-init + level-start flags
    g.lives, g.bonus_letters, g.utensils_mask, g.attack_v19, g.attack_phase = keep
