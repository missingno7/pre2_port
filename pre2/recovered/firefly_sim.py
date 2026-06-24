"""The full firefly-swarm simulation (1030:54AB) — animation + draw, for a LIVE native replacement.

`pre2.recovered.fireflies.draw_fireflies` recovered only the DRAW (enough for the faithful renderer to
show the swarm). This module recovers the per-frame ANIMATION too — the RNG-driven flocking toward the
target point — so Python can OWN the whole 54AB pass and the VM skips it (the perf win: the swarm no
longer runs as interpreted ASM every frame).

The pass is byte-entangled with shared state, so the simulation is a pure transform over a
:class:`FireflySimState` snapshot (the bridge reads/writes it). It MUST be byte-exact including the two
shared RNG generators — other game systems draw from the same `26CF`/`39DF`, so any drift in the call
count or math desyncs the whole game. The verify (pre2/probes/verify_firefly_sim.py) diffs the slots, BOTH
RNG seeds, the [0x6BC0]/[0x6BC1] scratch, and the drawn VRAM against the real ASM pass.

State (ds=1A0F):
  * slots: 20 x 8 bytes at 0x6EA9 — [x.w, y.w, vx.b, vy.b, timer.b, flags.b]; first word 0x55AA = dead.
  * RNG-A (26CF): a 16-bit LCG seed at 0x28C1 (`s = ror((s + 0x9248), 3)`), returns the low byte.
  * RNG-B (39DF): a 4-byte generator (word 0x2CEF + bytes 0x2CEC/0x2CED/0x2CEE), returns byte 0x2CED.
  * target: [0x4F1C]/[0x4F1E] (the point the swarm drifts toward); frame gate [0x6BD5]; scratch
    [0x6BC0]/[0x6BC1]; camera [0x2DE4]/[0x2DE6]; back page [0x2DD8].

Per slot [asm 54C0]: skip if dead; decrement the timer and, when it underflows, RECOMPUTE a new timer
((rng_a & 7)+3) and a new velocity aimed at the target (two rng_b draws for the vx/vy magnitude, one
rng_a for the vx, the vy from `min(0x20, abs(vy)*8 / abs(vx))`), with sign chosen toward the target and
flipped by flags&7. Then MOVE [asm 5573]: x += s8(vx) (undone on signed overflow), y += s8(vy). The DRAW
is delegated to :func:`pre2.recovered.fireflies.draw_fireflies` over the post-move slots.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from pre2.islands import oracle_link
from pre2.recovered.fireflies import Firefly, draw_fireflies

_NUM_SLOTS = 20
_SLOT = 8


def _s8(v: int) -> int:
    return v - 256 if v & 0x80 else v


def _s16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


@dataclass
class FireflySimState:
    slots: bytearray                 # 20 * 8 bytes (mutated in place)
    rng_a: int                       # word [0x28C1]
    rng_b: List[int]                 # [word 0x2CEF, byte 0x2CEC, byte 0x2CED, byte 0x2CEE]
    target_x: int                    # [0x4F1C] (signed)
    target_y: int                    # [0x4F1E] (signed)
    frame_gate: int                  # [0x6BD5]
    scratch: List[int]               # [byte 0x6BC0, byte 0x6BC1]
    cam_col: int                     # [0x2DE4] (signed)
    cam_row: int                     # [0x2DE6] (signed)
    page: int                        # [0x2DD8]
    draw: List[Firefly] = field(default_factory=list)   # filled by step_fireflies for the bridge


def _rng_a(st: FireflySimState) -> int:
    ax = (st.rng_a + 0x9248) & 0xFFFF
    ax = ((ax >> 3) | (ax << 13)) & 0xFFFF       # ror ax,1 x3
    st.rng_a = ax
    return ax & 0xFF


def _rng_b(st: FireflySimState) -> int:
    word, cec, ced, cee = st.rng_b
    word = (word + cec) & 0xFFFF                  # dx += [0x2CEC]
    dh = (word >> 8) & 0xFF
    cec = (cec + 3 + dh) & 0xFF                   # [0x2CEC] += 3 + dh
    ced = (ced + cee) & 0xFF
    ced = (ced + ced) & 0xFF
    ced = (ced + cec) & 0xFF                      # [0x2CED] = ((ced+cee)*2 + cec)
    cee = (cee ^ cec ^ ced) & 0xFF               # [0x2CEE] ^= cec ^ ced
    st.rng_b = [word, cec, ced, cee]
    return ced                                   # returns [0x2CED]


def _update_slot(st: FireflySimState, base: int) -> None:
    s = st.slots
    x = s[base] | (s[base + 1] << 8)
    if x == 0x55AA:                              # dead slot [asm 54C2]
        return
    timer = (s[base + 6] - 1) & 0xFF             # dec [si+6]
    s[base + 6] = timer
    if timer & 0x80:                             # js -> underflow -> recompute [asm 54D2]
        s[base + 6] = ((_rng_a(st) & 7) + 3) & 0xFF
        flags7 = s[base + 7] & 7
        # vx candidate aimed at target_x
        al = _rng_b(st) & 0xF
        dx = _s16(x) >> 3
        if not (st.target_x >= dx):              # cmp [0x4F1C],dx ; jge skip-neg
            al = (-al) & 0xFF
        if flags7:                               # test [si+7],7 ; jne -> neg
            al = (-al) & 0xFF
        st.scratch[0] = al
        # vy candidate aimed at target_y
        al = _rng_b(st) & 0xF
        y = s[base + 2] | (s[base + 3] << 8)
        dy = (_s16(y) >> 3) + 0x20
        if not (st.target_y >= dy):
            al = (-al) & 0xFF
        if flags7:
            al = (-al) & 0xFF
        st.scratch[1] = al
        # vx final = (rng_a & 0x1F), signed like scratch[0] [asm 5518]
        al = _rng_a(st) & 0x1F
        if _s8(st.scratch[0]) < 0:               # cmp [0x6BC0],0 ; jge skip
            al = (-al) & 0xFF
        s[base + 4] = al & 0xFF
        # vy final [asm 5529]
        if st.scratch[0] == 0:
            al = 2
        else:
            bl = st.scratch[0]
            if bl & 0x80:
                bl = (-bl) & 0xFF                # abs(scratch[0])
            ax = _s8(st.scratch[1])
            if ax < 0:
                ax = -ax                         # abs(vy candidate)
            ax = (ax << 3) & 0xFFFF              # *8
            al = (ax // bl) & 0xFF               # div bl -> AL quotient
        if al >= 0x20:                           # cmp al,0x20 ; jb skip -> min(al,0x20)
            al = 0x20
        if _s8(st.scratch[1]) < 0:               # cmp [0x6BC1],0 ; jge skip
            al = (-al) & 0xFF
        s[base + 5] = al & 0xFF
        # flags decrement, gated by the global frame counter [asm 5562]
        if not (st.frame_gate & 0xF) and (s[base + 7] & 7):
            s[base + 7] = (s[base + 7] - 1) & 0xFF
    # move [asm 5573]: x += s8(vx) with signed-overflow undo
    ax = _s8(s[base + 4]) & 0xFFFF
    res = (x + ax) & 0xFFFF
    of = ((x ^ res) & (ax ^ res) & 0x8000) != 0
    nx = x if of else res
    s[base] = nx & 0xFF
    s[base + 1] = (nx >> 8) & 0xFF
    y = s[base + 2] | (s[base + 3] << 8)
    ny = (y + (_s8(s[base + 5]) & 0xFFFF)) & 0xFFFF
    s[base + 2] = ny & 0xFF
    s[base + 3] = (ny >> 8) & 0xFF


@oracle_link("1030:54AB",
             "FULL firefly swarm pass (animation + draw) for the live replacement: advance all 20 slots "
             "of [0x6EA9] — decrement each timer, on underflow recompute (timer=(rng_a&7)+3, velocity "
             "aimed at target [0x4F1C]/[0x4F1E] via 2x rng_b + 1x rng_a, vy=min(0x20,abs(vy)*8/abs(vx))) "
             "then move x+=s8(vx) (signed-overflow undo) / y+=s8(vy). Drives the two SHARED RNGs 26CF "
             "([0x28C1] LCG) + 39DF ([0x2CEF/0x2CEC/0x2CED/0x2CEE]) byte-exact, plus the [0x6BC0]/[0x6BC1] "
             "scratch; the draw is delegated to draw_fireflies. Replaces the ASM 54AB outright.",
             "VERIFIED", merge_target="render_frame")
def step_fireflies(st: FireflySimState) -> FireflySimState:
    """Advance the whole swarm one frame and record the post-move draw list (for the bridge)."""
    for i in range(_NUM_SLOTS):
        _update_slot(st, i * _SLOT)
    st.draw = []
    for i in range(_NUM_SLOTS):
        b = i * _SLOT
        x = st.slots[b] | (st.slots[b + 1] << 8)
        if x == 0x55AA:
            continue
        y = st.slots[b + 2] | (st.slots[b + 3] << 8)
        st.draw.append((_s16(x), _s16(y), st.slots[b + 6]))
    return st


def render_step_into(st: FireflySimState, planes) -> None:
    """Apply :func:`draw_fireflies` for the swarm's post-move positions onto ``planes``."""
    draw_fireflies(planes, st.draw, st.cam_col, st.cam_row, st.page)
