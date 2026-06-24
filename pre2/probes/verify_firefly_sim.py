"""Lockstep-verify the full firefly simulation (1030:54AB) vs the real ASM pass.

At each 54AB entry: snapshot the sim state + the EGA planes (before). Run the real ASM pass to its ret
(55FB). Run the recovered ``step_fireflies`` on the snapshot and apply its draw to the before-planes.
Compare EVERY mutated byte: slots, both RNG seeds, the [0x6BC0]/[0x6BC1] scratch, and the drawn VRAM.
A single mismatch over many frames means the replacement would desync the game's shared RNG.
"""
import sys

sys.path.insert(0, ".")

from dos_re.cpu import IF
from dos_re.interrupts import deliver_interrupt
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from dos_re.runtime import enable_sound_blaster
from pre2.bridge.firefly_sim import read_firefly_sim_state
from pre2.recovered.firefly_sim import step_fireflies, render_step_into
from pre2.runtime import load_pre2_snapshot

_DATA = 0x1A0F
_ENTRY = 0x54AB
_RET = 0x55FB


def _grab(d):
    return [bytes(d[EGA_APERTURE + p * EGA_PLANE_STRIDE: EGA_APERTURE + p * EGA_PLANE_STRIDE + 0x10000])
            for p in range(4)]


def main(snap="artifacts/snapshot_pre2_20260624_140330", frames=40):
    rt = load_pre2_snapshot("assets/pre2.exe", snap, game_root="assets", native_replacements=False)
    cpu, dos = rt.cpu, rt.dos
    cpu.trace_enabled = False
    sb = enable_sound_blaster(rt, detection_only=True)
    pic = dos.pic
    clock = lambda: cpu.instruction_count / (6428 * 70)  # noqa: E731
    dos.time_source = clock
    tick = {"next": clock()}
    d = rt.program.memory.data

    def pump():
        now = clock()
        tp = 1.0 / max(1.0, dos.pit_channel0_hz())
        while now >= tick["next"]:
            pic.raise_irq(0)
            tick["next"] += tp
            if tick["next"] < now - 0.25:
                tick["next"] = now + tp
        if sb:
            sb.service()
        g = 0
        while cpu.get_flag(IF) and g < 64:
            nn = pic.acknowledge()
            if nn is None:
                break
            deliver_interrupt(rt, (0x08 + nn) if nn < 8 else (0x70 + nn - 8), max_steps=2_000_000)
            g += 1

    def _rb(off):
        return d[((_DATA << 4) + off) & 0xFFFFF]

    def _r16(off):
        a = ((_DATA << 4) + off) & 0xFFFFF
        return d[a] | (d[a + 1] << 8)

    s = cpu.s
    checked = 0
    fails = 0
    for _ in range(20_000_000):
        if cpu.instruction_count % 1500 == 0:
            pump()
        if s.cs == 0x1030 and s.ip == _ENTRY:
            st = read_firefly_sim_state(cpu.mem)
            before = _grab(d)
            sp0 = s.sp
            guard = 0
            while guard < 2_000_000:
                cpu.step()
                guard += 1
                if s.cs == 0x1030 and s.ip == _RET and s.sp >= sp0:
                    break
            # ASM results
            asm_slots = bytes(d[((_DATA << 4) + 0x6EA9): ((_DATA << 4) + 0x6EA9) + 160])
            asm_a = _r16(0x28C1)
            asm_b = (_r16(0x2CEF), _rb(0x2CEC), _rb(0x2CED), _rb(0x2CEE))
            asm_scr = (_rb(0x6BC0), _rb(0x6BC1))
            after = _grab(d)
            # recovered results
            step_fireflies(st)
            planes = [bytearray(b) for b in before]
            render_step_into(st, planes)
            rec_b = (st.rng_b[0], st.rng_b[1], st.rng_b[2], st.rng_b[3])
            vram_diff = sum(1 for p in range(4) for o in range(0x10000) if planes[p][o] != after[p][o])
            ok = (bytes(st.slots) == asm_slots and st.rng_a == asm_a and rec_b == asm_b
                  and (st.scratch[0], st.scratch[1]) == asm_scr and vram_diff == 0)
            checked += 1
            if not ok:
                fails += 1
                print(f"  frame {checked} MISMATCH: slots={bytes(st.slots)==asm_slots} "
                      f"a={st.rng_a==asm_a}({hex(st.rng_a)}/{hex(asm_a)}) b={rec_b==asm_b} "
                      f"scr={(st.scratch[0],st.scratch[1])==asm_scr} vram_diff={vram_diff}")
                if fails >= 5:
                    break
            if checked >= frames:
                break
        cpu.step()

    print(f"checked {checked} pass(es), {fails} mismatch(es)")
    print("FIREFLY_SIM: PASS" if checked and fails == 0 else "FIREFLY_SIM: FAIL")
    return 0 if (checked and fails == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
