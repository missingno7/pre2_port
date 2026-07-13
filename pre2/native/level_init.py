"""The native level-init / re-init sequence — the transition spine's foundation.

main()'s level-init at ``1030:01cf`` composes ``native_level_load`` (3ed6) + **5237** re-init +
``native_player_init`` (55fc) + **3af2** camera, then re-seeds the RNG. ``5237``/``3af2`` are *shared* with the
respawn (``4F6C``), so recovering them here serves cold-boot, every level-change, and the respawn at once.

Render side effects (the ``0ba0`` VGA palette load, the ``52d2`` sprite-graphics blit into the ``[0x2dda]`` level
segment, the ``3af2`` initial screen draw) are the renderer's job — this module owns the **gameplay-state (DGROUP)
contract**. See the ``pre2-main-flow-spine`` + ``pre2-level-state-machine`` memories.
"""
from __future__ import annotations

from pre2.native.camera_scroll import (SCROLL_DONE_FLAG, _h_follow, _sar16, _v_follow, _wb_cs,
                                        apply_camera_pan)
from pre2.gaps import Pre2HybridGap
from pre2.native.level_load import native_level_load, native_player_init
from pre2.native.state import DATA_SEG
from pre2.views.dgroup_view import PlayerGlobals, PlayerView
from pre2.recovered.prng import rng_lcg

_DS = DATA_SEG << 4


def native_3af2(state) -> None:
    """[asm 3af2] The level-start camera-init: reset the camera cells, then snap-scroll the camera onto the player.

    The scroll loop (``5634``) is just the recovered per-frame camera-follow (``5649`` -> 57a8 H + 5663 V) run to
    convergence, so this reuses ``native_camera_follow`` rather than re-deriving the centre/clamp — the camera
    settles at ``clamp(player_tile - 9/-7, 0, level-edge)`` exactly as the follow's window + edge-clamp dictate.
    The initial screen tile-draw and the ``[0x2dba]``/``[0x2de8]``/``[0x2df2]`` render-pointer state are the
    renderer's job and are not produced here."""
    d = state.data
    g, pv = PlayerGlobals(state), PlayerView(state)
    g.cam_col_word = 0; g.cam_row_word = 0; g.col_ring = 0; g.unk_2DEA = 0   # [asm 3afe-3b07] reset camera cells
    for _ in range(8000):                                           # [asm 3b35] 5634: snap-scroll to convergence
        _wb_cs(state, SCROLL_DONE_FLAG, 1)                          # [asm 5634] set the snap flag (forces dl=0x10)
        if g.unk_6BD9 == 0 and not (g.level_flags & 2):            # [asm 564e/5655] 5649 gate
            _h_follow(state)                                       # [asm 565c] 57a8 horizontal follow
        _v_follow(state)                                           # [asm 565f] 5663 vertical follow
        _wb_cs(state, SCROLL_DONE_FLAG, 0)                          # [asm 563d] clear the snap flag
        if g.unk_6BED == 0:                                        # [asm 3b38] both axes idle -> converged (word 0x6bed)
            break
    if not (g.level_flags & 2):                                     # [asm 3b41] (not the gated mode)
        px_tile = _sar16(pv.x, 4)
        cam_x = g.cam_col_word                                      # [asm 3b48-3b4f] player tile - camera cell
        if ((px_tile - cam_x) & 0xFFFF) >= 0xC:                    # [asm 3b53] still > 0xc cols right -> centre it
            for _ in range(0xA):                                   # [asm 3b58] pan right x10
                apply_camera_pan(state, "right")
    g.grid_dirty = 1                                                # [asm 3b60]
    g.grid_dirty_token = 0x55AA                                     # [asm 3b65] [0x2de0]=0x55aa


def native_52d2(state) -> None:
    """[asm 52D2, called from 5237 @5292] Restore the pristine PROXIMITY-SCENERY map blocks: walk the 41CA-built
    save bank at ``[0x2875]:0`` (``{word dest_map_off, byte rows, byte width, width*rows tiles}``, 0xFFFF-
    terminated) and copy each saved block back over the level map (``es=[0x2DDA]``, one row per 0x100 stride).
    This un-collapses every fired earthquake/breakable-scenery trigger on respawn AND at level start.

    NOT render: ``[0x2DDA]`` is the COLLISION map. This was misclassified as a sprite blit and skipped — the
    only state the gameplay tick mutates outside DGROUP, so the tick digest never saw it. Witness: demo
    210723 (L0xD) — die on collapsed scenery, respawn, walk back: the VM restored the 74 collapsed bytes at
    the respawn (tick 653) while native kept them, and the stale tile stopped the player as a phantom wall
    at tick 788 (the reported "camera inaccuracy")."""
    from pre2.views.dgroup_view import ProximityView, SegmentBackend
    v = ProximityView(state)
    # The 41CA bank lives in VOLATILE [0x2875] scratch (the bump-allocator load top, never bumped past the
    # bank), so the original relies on nothing overwriting it for the life of the level. Its content is a pure
    # function of the [0x83F3] trigger table (41CA saves one block per LIVE trigger; an all-dead table yields a
    # bank of just the 0xFFFF terminator), and native_5237 restores [0x83F3] from the pristine [0x9203] backup
    # at 5251 BEFORE this 5292 call, so the trigger table here is authoritative. When every trigger is dead
    # there is nothing to restore -> return without reading the scratch at all: byte-identical to walking a
    # correct empty bank (immediate 0xFFFF), and robust to the scratch being clobbered on a no-trigger level
    # (observed once under widescreen play on idx 1 -- native_gap_20260706_121819: the 0x6caf bank read 0x0000
    # while [0x83F3] was correctly all-0xFFFF). Fail loud below only for an unterminated bank on a level that
    # DOES have live triggers (a genuine loss of real collapsed-block data).
    if all(t.dead for t in v.triggers):
        return
    bank = SegmentBackend(state, v.bank_seg)                         # [asm 52d8] ds = [0x2875]
    game_map = SegmentBackend(state, v.map_seg)                      # [asm 52d4] es = [0x2DDA]
    si = 0                                                           # [asm 52dc]
    for _ in range(0x10):                                            # bank holds at most 15 entries (41CA cx=0xf)
        dest = bank.rw(si)                                           # [asm 52de] the block's map offset
        if dest == 0xFFFF:                                           # [asm 52e0] terminator
            return
        rows, width = bank.rb(si + 2), bank.rb(si + 3)               # [asm 52e5] al=rows, ah=width
        si += 4                                                      # [asm 52e8]
        for _r in range(rows):                                       # [asm 52eb-52f7]
            for k in range(width):                                   # rep movsb (one map row)
                game_map.wb(dest + k, bank.rb(si + k))
            si += width
            dest = (dest + 0x100) & 0xFFFF                           # [asm 52f1] next map row
    raise Pre2HybridGap("52D2 scenery-restore: no 0xFFFF terminator within 15 entries on a level WITH live "
                        "[0x83F3] triggers -- the [0x2875] trigger bank was built (41CA) but got corrupted, "
                        "so real collapsed-scenery block data is lost")


def native_5237(state) -> None:
    """[asm 5237] The level / respawn RE-INIT (level-init @01d8, respawn ``4F6C`` @4fbf): zero the timer block,
    restore the pristine double-buffer from its backup, re-init the object pool + player (``native_player_init``),
    restore the pristine proximity-scenery map blocks (``native_52d2``), seed the decor-RNG table, and reset the
    energy / boss / scroll-script state. The ``0ba0`` sub-call (VGA palette via int 10h) is render and skipped."""
    d = state.data
    g, pv = PlayerGlobals(state), PlayerView(state)
    d[_DS + 0x6BC4:_DS + 0x6BC4 + 0x48] = b"\x00" * 0x48              # [asm 5247] zero the timer/state block
    d[_DS + 0x815E:_DS + 0x815E + 0x10A5] = bytes(d[_DS + 0x9203:_DS + 0x9203 + 0x10A5])  # [asm 5251] restore dbl-buffer
    pv.run_flag = 0                                                  # [asm 525c]
    state.ww(0x2DE0, 0x55AA)                                         # [asm 525f]
    state.ww(0x6BBE, 0x4F76)                                         # [asm 5265] popup-ring head
    native_player_init(state)                                       # [asm 526e] 55fc (ax=0xffff)
    # [asm 5271] 0ba0 VGA palette (int 10h) — render, skipped (no DGROUP)
    a, b, c, dd = state.rb(0x2CEC), state.rb(0x2CED), state.rb(0x2CEE), state.rw(0x2CEF)  # [5274] fill [0x6ca9]
    di = 0x6CA9
    for _ in range(0x100):
        a, b, c, dd, _r1 = rng_lcg(a, b, c, dd)                     # [asm 527a] dh (advances the RNG, discarded)
        a, b, c, dd, r2 = rng_lcg(a, b, c, dd)                      # [asm 527f] ah = al = the 2nd return byte
        state.ww(di, (r2 << 8) | r2); di += 2                      # [asm 5284] stosw (the byte duplicated)
    state.wb(0x2CEC, a); state.wb(0x2CED, b); state.wb(0x2CEE, c); state.ww(0x2CEF, dd)   # advanced RNG state back
    for i in range(0x50):                                           # [asm 5287] [0x6ea9..] = 0x55aa x 0x50
        state.ww(0x6EA9 + i * 2, 0x55AA)
    native_52d2(state)                                               # [asm 5292] restore the pristine scenery map blocks
    d[_DS + 0x7DE6:_DS + 0x7DE6 + 0x78] = b"\xFF" * 0x78             # [asm 5295]
    d[_DS + 0x7DAF:_DS + 0x7DAF + 0x37] = b"\xFF" * 0x37             # [asm 52a0]
    g.energy = 3                                                     # [asm 52a8] energy / hearts
    state.ww(0xA517, 0xFFFF)                                         # [asm 52ad] boss state reset
    state.ww(0x2DBC, state.rw((g.level * 2 + 0x2D40) & 0xFFFF))     # [asm 52b3] per-level scroll-script ptr
    state.ww(0x2DBE, 0)                                             # [asm 52c2]


def native_level_init(state, *, game_root: str) -> None:
    """[asm 01cf..020f] The full level-init sequence main() runs to start (or change to) a level — the transition
    spine's foundation, re-run on every level-change and sharing 5237/3af2 with the respawn. Loads the level
    ``[0x2d8a]`` and composes the recovered halves: ``native_level_load`` (3ed6) + ``native_5237`` (re-init) +
    ``native_player_init`` (55fc) + ``native_3af2`` (camera-init), then re-seeds the RNG to the fixed level-start
    value + clears the per-level flags. The render sub-calls (0ba0 palette, 454e sprite-save, 3a27 scroll-copy,
    44fb CRTC) are the renderer's job. Each composed leaf is verified byte-exact against the ASM individually."""
    g = PlayerGlobals(state)
    level = g.level
    native_level_load(state, level, game_root=game_root)            # [asm 01d2] 3ed6
    # [asm 01d5] 0ba0 VGA palette load — render
    native_5237(state)                                             # [asm 01d8] 5237 re-init
    native_player_init(state)                                      # [asm 01e0] 55fc
    native_3af2(state)                                             # [asm 01e3] camera-init
    # [asm 01e6-01ec] 454e sprite-save / 3a27 scroll-copy / 44fb CRTC — render
    g.spawn_offset_ring = 0                                        # [asm 01ef] [0xa341]=0 (word)
    state.wb(0x6BCC, 0)                                            # [asm 01f5]
    state.wb(0x2CEC, 5); state.wb(0x2CED, 0x22); state.wb(0x2CEE, 0x86)  # [asm 01fa] re-seed the RNG to the
    state.wb(0x2CEF, 0x8D); state.wb(0x2CF0, 0xE5)                 # fixed level-start (a,b,c,d=0xe58d)
    state.wb(0x2874, 0)                                            # [asm 020f]


def native_level_start(state, *, game_root: str) -> None:
    """[asm main 0x13e..0x0155] The full LEVEL-START sequence main() runs when ENTERING a level — from the menu at
    game start, and re-run on each level-END transition. It is ``native_level_init`` (the 447d/01cf load + re-init)
    followed by the SEPARATE 0x0141-0x0155 level-start block: reset lives ``[0x27d8]=2``, the projectile
    damage/tolerance ``[0x7b19]=0x14``, the BONUS-letter / utensil masks, and clear the level-state flags. Those
    writes are NOT part of native_level_init (01cf) — the menu->gameplay handoff and cold boot must run them too,
    else lives reads 0, combat damage is wrong, etc. (This is the block ``native_level_end`` already applied inline.)"""
    native_level_init(state, game_root=game_root)                 # [asm 013e: 447d] load + re-init the level
    g = PlayerGlobals(state)
    g.lives = 2                                                   # [asm 0141]
    g.bonus_letters = 0                                           # [asm 0146] BONUS-letters mask
    g.attack_v19 = 0x14                                           # [asm 014b] projectile damage / hit tolerance
    g.attack_phase = 0                                            # [asm 0150]
    g.utensils_mask = 0                                           # [asm 0155] utensils mask
    g.level_end_mode = 0; g.respawn_state = 0; g.end_signal = 0   # level-state flags are clear in gameplay
