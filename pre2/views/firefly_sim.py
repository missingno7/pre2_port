"""Bridge: read/write the firefly-swarm simulation state (1030:54AB). ds=1A0F throughout. Layout only."""
from __future__ import annotations

from pre2.recovered.firefly_sim import FireflySimState

_DATA = 0x1A0F
_SLOTS = 0x6EA9
_SLOTS_LEN = 20 * 8


def _r16(d, off):
    a = ((_DATA << 4) + off) & 0xFFFFF
    return d[a] | (d[(a + 1) & 0xFFFFF] << 8)


def _w16(d, off, v):
    a = ((_DATA << 4) + off) & 0xFFFFF
    d[a] = v & 0xFF
    d[(a + 1) & 0xFFFFF] = (v >> 8) & 0xFF


def _rb(d, off):
    return d[((_DATA << 4) + off) & 0xFFFFF]


def _wb(d, off, v):
    d[((_DATA << 4) + off) & 0xFFFFF] = v & 0xFF


def _s16(v):
    return v - 0x10000 if v & 0x8000 else v


def read_firefly_sim_state(mem) -> FireflySimState:
    d = mem.data
    sbase = ((_DATA << 4) + _SLOTS) & 0xFFFFF
    return FireflySimState(
        slots=bytearray(d[sbase:sbase + _SLOTS_LEN]),
        rng_a=_r16(d, 0x28C1),
        rng_b=[_r16(d, 0x2CEF), _rb(d, 0x2CEC), _rb(d, 0x2CED), _rb(d, 0x2CEE)],
        target_x=_s16(_r16(d, 0x4F1C)),
        target_y=_s16(_r16(d, 0x4F1E)),
        frame_gate=_rb(d, 0x6BD5),
        scratch=[_rb(d, 0x6BC0), _rb(d, 0x6BC1)],
        cam_col=_s16(_r16(d, 0x2DE4)),
        cam_row=_s16(_r16(d, 0x2DE6)),
        page=_r16(d, 0x2DD8),
    )


def write_firefly_sim_state(mem, st: FireflySimState) -> None:
    """Write back every byte the ASM pass mutates (the contract): slots, both RNG seeds, scratch."""
    d = mem.data
    sbase = ((_DATA << 4) + _SLOTS) & 0xFFFFF
    d[sbase:sbase + _SLOTS_LEN] = bytes(st.slots)
    _w16(d, 0x28C1, st.rng_a)
    _w16(d, 0x2CEF, st.rng_b[0])
    _wb(d, 0x2CEC, st.rng_b[1])
    _wb(d, 0x2CED, st.rng_b[2])
    _wb(d, 0x2CEE, st.rng_b[3])
    _wb(d, 0x6BC0, st.scratch[0])
    _wb(d, 0x6BC1, st.scratch[1])
