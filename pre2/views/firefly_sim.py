"""Bridge: read/write the firefly-swarm simulation state (1030:54AB). ds=1A0F throughout. Layout only."""
from __future__ import annotations

from pre2.recovered.firefly_sim import FireflySimState
from pre2.native.dgroup_offsets import (
    CAM_COL, CAM_ROW, FIREFLY_SCRATCH_A, FIREFLY_SCRATCH_B, FRAME_TIMER, PLAYER_SLOT, PLAYER_Y, RENDER_PAGE, RNG_ROTATE, RNG_STATE)

_DATA = 0x1A0F
_SLOTS = 0x6EA9
_SLOTS_LEN = 20 * 8


def _s16(v):
    return v - 0x10000 if v & 0x8000 else v


def read_firefly_sim_state(mem) -> FireflySimState:
    return FireflySimState(                              # via mem's accessors (through the backend seam, Phase 4)
        slots=bytearray(mem.rb(_SLOTS + k) for k in range(_SLOTS_LEN)),
        rng_a=mem.rw(RNG_ROTATE),
        rng_b=[mem.rw(RNG_STATE + 3), mem.rb(RNG_STATE), mem.rb(RNG_STATE + 1), mem.rb(RNG_STATE + 2)],
        target_x=_s16(mem.rw(PLAYER_SLOT)),
        target_y=_s16(mem.rw(PLAYER_Y)),
        frame_gate=mem.rb(FRAME_TIMER),
        scratch=[mem.rb(FIREFLY_SCRATCH_A), mem.rb(FIREFLY_SCRATCH_B)],
        cam_col=_s16(mem.rw(CAM_COL)),
        cam_row=_s16(mem.rw(CAM_ROW)),
        page=mem.rw(RENDER_PAGE),
    )


def write_firefly_sim_state(mem, st: FireflySimState) -> None:
    """Write back every byte the ASM pass mutates (the contract): slots, both RNG seeds, scratch."""
    for k in range(_SLOTS_LEN):
        mem.wb(_SLOTS + k, st.slots[k])
    mem.ww(RNG_ROTATE, st.rng_a)
    mem.ww(RNG_STATE + 3, st.rng_b[0])
    mem.wb(RNG_STATE, st.rng_b[1])
    mem.wb(RNG_STATE + 1, st.rng_b[2])
    mem.wb(RNG_STATE + 2, st.rng_b[3])
    mem.wb(FIREFLY_SCRATCH_A, st.scratch[0])
    mem.wb(FIREFLY_SCRATCH_B, st.scratch[1])
