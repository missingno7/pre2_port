"""The PRE2 death / respawn / level-state handlers — the 4C69 dispatch targets, VM-independent native source.

``native_4f6c`` is the respawn-to-checkpoint handler (``[0x6be4]==1``): it plays the death-bounce, then restores
the level to its checkpoint and continues the gameplay loop. The death (5063) / game-over (5034) / level-end
(4F65) handlers — which return carry to main's level-change at 0x12f — are the remaining gaps. See the
``pre2-level-state-machine`` memory for the full map.
"""
from __future__ import annotations

from pre2.checkpoints.common import Pre2HybridGap
from pre2.native.level_init import native_3af2, native_5237
from pre2.native.loop import native_death_bounce_509d
from pre2.native.state import DATA_SEG

_DS = DATA_SEG << 4
# [asm 4fe1-4fff] effect-sprite types (relative to 0x35) the checkpoint restore must NOT overwrite — the
# re-init's freshly-spawned transient sprites are kept rather than rolled back to the snapshot.
_TRANSIENT_TYPES = {0xD, 0x2C, 0x41, 0xA9, 0xAA, 0xB6, 0xE0}


def native_51df(state) -> None:
    """[asm 51DF] Respawn cleanup: zero the object-backup scratch ``[0x6c12..+0x71]`` + ``[0x6c9e]``."""
    d = state.data
    d[_DS + 0x6C12:_DS + 0x6C12 + 0x71] = b"\x00" * 0x71            # [asm 51e2-51e7]
    d[_DS + 0x6C9E] = 0; d[_DS + 0x6C9F] = 0                        # [asm 51e9] [0x6c9e] = 0 (word)


def native_4f6c(state):
    """[asm 4F6C] The respawn-to-checkpoint handler (4C69's ``[0x6be4]==1`` target). Plays the death-bounce
    (509d), then — unless a real death is pending (``[0x2879]`` or ``[0x6be5]``) — snapshots the live
    effect-sprite-source + active-flag tables, re-inits the level (5237), drops the player at the checkpoint
    ``[0x6bad]``/``[0x6baf]``, re-inits the camera (3af2), restores the snapshot (keeping the re-init's transient
    effect sprites + re-freeing the slots that were dead), and cleans up (51df). Returns with NO level change —
    the gameplay loop continues. The pending-death tail (506c, carry -> main's 0x12f game-over) is not yet here.

    GENERATOR: ``yield``s once per death-bounce frame (60 frames) so the caller renders the whole arc, THEN runs
    the checkpoint restore (instantaneous — the screen jumps to the checkpoint on the next rendered frame). Drive
    to completion to apply the respawn (``for _ in native_4f6c(state): render(...)``).

    Verified byte-exact end-to-end vs the ASM: pre2/probes/probe_native_respawn.py drives the blocking ASM 4F6C
    through the timer/retrace machinery and diffs DGROUP at the RET 0x5033 -> 0 diffs (render/timing excluded);
    pre2/probes/probe_native_respawn_anim.py additionally diffs EACH bounce frame vs the ASM 509d loop."""
    d = state.data

    def rb(o): return d[_DS + (o & 0xFFFF)]

    def rw(o): return d[_DS + (o & 0xFFFF)] | (d[_DS + ((o + 1) & 0xFFFF)] << 8)

    def wb(o, v): d[_DS + (o & 0xFFFF)] = v & 0xFF

    def ww(o, v): wb(o, v); wb(o + 1, v >> 8)

    yield from native_death_bounce_509d(state)                      # [asm 4f6c] 509d death-bounce (per-frame)
    if rb(0x2879) != 0 or rb(0x6BE5) != 0:                          # [asm 4f6f-4f80] a real death is pending
        raise Pre2HybridGap("native respawn 4F6C: the pending-death tail (506c, game-over) is not recovered")

    # [asm 4f91-4fa6] snapshot the 0x46 effect-sprite source values [0x8f1d]+4 (stride 7) -> [0x6c12] (stride 2)
    si, di = 0x8F1D, 0x6C12
    for _ in range(0x46):
        ww(di, rw(si + 4)); si += 7; di += 2
    ww(di, 0x55AA)                                                 # [asm 4fa3] end marker
    # [asm 4fa7-4fbd] snapshot the 0x50 active flags [0x8c8d]+3 (stride 5) -> [0xa2a8] (1 = live, 0 = free)
    si, di = 0x8C8D, 0xA2A8
    for _ in range(0x50):
        wb(di, 0 if rw(si + 3) == 0xFFFF else 1); si += 5; di += 1

    native_5237(state)                                            # [asm 4fbf] 5237 level re-init
    ww(0x4F1C, rw(0x6BAD)); ww(0x4F1E, rw(0x6BAF))                # [asm 4fc2-4fcb] player -> the checkpoint
    native_3af2(state)                                           # [asm 4fce] 3af2 camera-init

    # [asm 4fd1-500a] restore the effect-sprite sources, skipping slots whose re-init type is transient
    di, si = 0x8F1D, 0x6C12
    for _ in range(0x46):
        ax = rw(si); si += 2
        if ((rw(di + 4) - 0x35) & 0xFF) not in _TRANSIENT_TYPES:
            ww(di + 4, ax)
        di += 7
    # [asm 500c-5022] re-free any slot the snapshot recorded as dead -> [0x8c8d]+3 = 0xffff
    di, si = 0x8C8D, 0xA2A8
    for _ in range(0x50):
        if rb(si) == 0:
            ww(di + 3, 0xFFFF)
        si += 1; di += 5

    native_51df(state)                                           # [asm 5024] cleanup
    # [asm 5030-5033] ax=0; clc; ret — no level change, the gameplay loop continues
