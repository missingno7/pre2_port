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
from pre2.native.level_load import native_level_load, native_player_init
from pre2.native.state import DATA_SEG
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
    for o in (0x2DE4, 0x2DE6, 0x2DE8, 0x2DEA):                       # [asm 3afe-3b07] reset the camera cells
        d[_DS + o] = 0; d[_DS + o + 1] = 0
    for _ in range(8000):                                           # [asm 3b35] 5634: snap-scroll to convergence
        _wb_cs(state, SCROLL_DONE_FLAG, 1)                          # [asm 5634] set the snap flag (forces dl=0x10)
        if d[_DS + 0x6BD9] == 0 and not (d[_DS + 0x8166] & 2):      # [asm 564e/5655] 5649 gate
            _h_follow(state)                                       # [asm 565c] 57a8 horizontal follow
        _v_follow(state)                                           # [asm 565f] 5663 vertical follow
        _wb_cs(state, SCROLL_DONE_FLAG, 0)                          # [asm 563d] clear the snap flag
        if d[_DS + 0x6BEE] == 0 and d[_DS + 0x6BED] == 0:           # [asm 3b38] both axes idle -> converged
            break
    if not (d[_DS + 0x8166] & 2):                                   # [asm 3b41] (not the gated mode)
        px_tile = _sar16(d[_DS + 0x4F1C] | (d[_DS + 0x4F1D] << 8), 4)
        cam_x = d[_DS + 0x2DE4] | (d[_DS + 0x2DE5] << 8)            # [asm 3b48-3b4f] player tile - camera cell
        if ((px_tile - cam_x) & 0xFFFF) >= 0xC:                    # [asm 3b53] still > 0xc cols right -> centre it
            for _ in range(0xA):                                   # [asm 3b58] pan right x10
                apply_camera_pan(state, "right")
    d[_DS + 0x2DF4] = 1                                             # [asm 3b60]
    d[_DS + 0x2DE0] = 0xAA; d[_DS + 0x2DE1] = 0x55                  # [asm 3b65] [0x2de0]=0x55aa


def native_5237(state) -> None:
    """[asm 5237] The level / respawn RE-INIT (level-init @01d8, respawn ``4F6C`` @4fbf): zero the timer block,
    restore the pristine double-buffer from its backup, re-init the object pool + player (``native_player_init``),
    seed the decor-RNG table, and reset the energy / boss / scroll-script state. The two render sub-calls — ``0ba0``
    (VGA palette via int 10h) and ``52d2`` (sprite blit into the ``[0x2dda]`` segment) — touch no DGROUP and are
    the renderer's job, so they are not run here."""
    d = state.data

    def rb(o: int) -> int:
        return d[_DS + (o & 0xFFFF)]

    def rw(o: int) -> int:
        return d[_DS + (o & 0xFFFF)] | (d[_DS + ((o + 1) & 0xFFFF)] << 8)

    def wb(o: int, v: int) -> None:
        d[_DS + (o & 0xFFFF)] = v & 0xFF

    def ww(o: int, v: int) -> None:
        wb(o, v); wb(o + 1, v >> 8)

    d[_DS + 0x6BC4:_DS + 0x6BC4 + 0x48] = b"\x00" * 0x48              # [asm 5247] zero the timer/state block
    d[_DS + 0x815E:_DS + 0x815E + 0x10A5] = bytes(d[_DS + 0x9203:_DS + 0x9203 + 0x10A5])  # [asm 5251] restore dbl-buffer
    wb(0x4F2C, 0)                                                     # [asm 525c]
    ww(0x2DE0, 0x55AA)                                               # [asm 525f]
    ww(0x6BBE, 0x4F76)                                              # [asm 5265]
    native_player_init(state)                                       # [asm 526e] 55fc (ax=0xffff)
    # [asm 5271] 0ba0 VGA palette (int 10h) — render, skipped (no DGROUP)
    a, b, c, dd = rb(0x2CEC), rb(0x2CED), rb(0x2CEE), rw(0x2CEF)     # [asm 5274] fill [0x6ca9] with 0x100 RNG words
    di = 0x6CA9
    for _ in range(0x100):
        a, b, c, dd, _r1 = rng_lcg(a, b, c, dd)                     # [asm 527a] dh (advances the RNG, discarded)
        a, b, c, dd, r2 = rng_lcg(a, b, c, dd)                      # [asm 527f] ah = al = the 2nd return byte
        ww(di, (r2 << 8) | r2); di += 2                            # [asm 5284] stosw (the byte duplicated)
    wb(0x2CEC, a); wb(0x2CED, b); wb(0x2CEE, c); ww(0x2CEF, dd)      # write the advanced RNG state back
    for i in range(0x50):                                           # [asm 5287] [0x6ea9..] = 0x55aa x 0x50
        ww(0x6EA9 + i * 2, 0x55AA)
    # [asm 5292] 52d2 sprite-graphics blit into the [0x2dda] level seg — render, skipped (no DGROUP)
    d[_DS + 0x7DE6:_DS + 0x7DE6 + 0x78] = b"\xFF" * 0x78             # [asm 5295]
    d[_DS + 0x7DAF:_DS + 0x7DAF + 0x37] = b"\xFF" * 0x37             # [asm 52a0]
    wb(0x27D6, 3)                                                    # [asm 52a8] energy / hearts
    ww(0xA517, 0xFFFF)                                              # [asm 52ad] boss state reset
    ww(0x2DBC, rw((rb(0x2D8A) * 2 + 0x2D40) & 0xFFFF))             # [asm 52b3] per-level scroll-script ptr
    ww(0x2DBE, 0)                                                    # [asm 52c2]


def native_level_init(state, *, game_root: str) -> None:
    """[asm 01cf..020f] The full level-init sequence main() runs to start (or change to) a level — the transition
    spine's foundation, re-run on every level-change and sharing 5237/3af2 with the respawn. Loads the level
    ``[0x2d8a]`` and composes the recovered halves: ``native_level_load`` (3ed6) + ``native_5237`` (re-init) +
    ``native_player_init`` (55fc) + ``native_3af2`` (camera-init), then re-seeds the RNG to the fixed level-start
    value + clears the per-level flags. The render sub-calls (0ba0 palette, 454e sprite-save, 3a27 scroll-copy,
    44fb CRTC) are the renderer's job. Each composed leaf is verified byte-exact against the ASM individually."""
    d = state.data
    level = d[_DS + 0x2D8A]
    native_level_load(state, level, game_root=game_root)            # [asm 01d2] 3ed6
    # [asm 01d5] 0ba0 VGA palette load — render
    native_5237(state)                                             # [asm 01d8] 5237 re-init
    native_player_init(state)                                      # [asm 01e0] 55fc
    native_3af2(state)                                             # [asm 01e3] camera-init
    # [asm 01e6-01ec] 454e sprite-save / 3a27 scroll-copy / 44fb CRTC — render
    d[_DS + 0xA341] = 0; d[_DS + 0xA342] = 0                        # [asm 01ef] [0xa341]=0 (word)
    d[_DS + 0x6BCC] = 0                                            # [asm 01f5]
    d[_DS + 0x2CEC] = 5; d[_DS + 0x2CED] = 0x22; d[_DS + 0x2CEE] = 0x86  # [asm 01fa] re-seed the RNG to the
    d[_DS + 0x2CEF] = 0x8D; d[_DS + 0x2CF0] = 0xE5                  # fixed level-start (a,b,c,d=0xe58d)
    d[_DS + 0x2874] = 0                                            # [asm 020f]
