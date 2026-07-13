"""Bridge: read/write the firefly-swarm simulation state (1030:54AB). ds=1A0F throughout. Layout only."""
from __future__ import annotations

from pre2.recovered.firefly_sim import FireflySimState

_DATA = 0x1A0F
_SLOTS = 0x6EA9
_SLOTS_LEN = 20 * 8


def _s16(v):
    return v - 0x10000 if v & 0x8000 else v


def read_firefly_sim_state(mem) -> FireflySimState:
    return FireflySimState(                              # via mem's accessors (through the backend seam, Phase 4)
        slots=bytearray(mem.rb(_SLOTS + k) for k in range(_SLOTS_LEN)),
        rng_a=mem.rw(0x28C1),
        rng_b=[mem.rw(0x2CEF), mem.rb(0x2CEC), mem.rb(0x2CED), mem.rb(0x2CEE)],
        target_x=_s16(mem.rw(0x4F1C)),
        target_y=_s16(mem.rw(0x4F1E)),
        frame_gate=mem.rb(0x6BD5),
        scratch=[mem.rb(0x6BC0), mem.rb(0x6BC1)],
        cam_col=_s16(mem.rw(0x2DE4)),
        cam_row=_s16(mem.rw(0x2DE6)),
        page=mem.rw(0x2DD8),
    )


def write_firefly_sim_state(mem, st: FireflySimState) -> None:
    """Write back every byte the ASM pass mutates (the contract): slots, both RNG seeds, scratch."""
    for k in range(_SLOTS_LEN):
        mem.wb(_SLOTS + k, st.slots[k])
    mem.ww(0x28C1, st.rng_a)
    mem.ww(0x2CEF, st.rng_b[0])
    mem.wb(0x2CEC, st.rng_b[1])
    mem.wb(0x2CED, st.rng_b[2])
    mem.wb(0x2CEE, st.rng_b[3])
    mem.wb(0x6BC0, st.scratch[0])
    mem.wb(0x6BC1, st.scratch[1])
