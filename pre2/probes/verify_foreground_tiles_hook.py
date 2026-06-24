"""Whole-state lockstep proof for the foreground-tile live replacement (1030:3732 -> ret 37F6).

At each 3732 entry: snapshot the machine; run the ASM to its ret and capture the whole state (all memory +
regs + the EGA register state); restore; run the recovered ``foreground_tiles`` replacement; diff. Byte-exact
= the recovered pass IS a safe live ASM replacement (the select+blit draw + the EGA register exit state +
es=[0x2DDA]). Drives the 110346 witness (player walking behind a foreground bush).
"""
import copy
import sys
sys.path.insert(0, ".")

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.cpu import IF
from dos_re.interrupts import deliver_interrupt
from dos_re.runtime import enable_sound_blaster
from pre2.bridge.foreground_tiles import read_foreground_state
from pre2.checkpoints.foreground_tiles import foreground_tiles
from pre2.runtime import load_pre2_snapshot

CS, ENTRY, EXIT = 0x1030, 0x3732, 0x37F6
_REGS = ("ax", "bx", "cx", "dx", "si", "di", "bp", "sp", "ds", "es", "ss", "cs", "ip", "flags")
_EGA = ("ega_map_mask", "ega_write_mode", "ega_logical_op", "ega_data_rotate", "ega_read_mode")


def _regs(s):
    return {r: getattr(s, r) for r in _REGS}


def _ega(mem):
    return {a: getattr(mem, a, None) for a in _EGA}


def main(snap="artifacts/snapshot_pre2_20260624_110346", max_calls=8):
    rt = load_pre2_snapshot("assets/pre2.exe", snap, game_root="assets", native_replacements=True)
    cpu, dos, m = rt.cpu, rt.dos, rt.cpu.mem
    cpu.trace_enabled = False
    sb = enable_sound_blaster(rt, detection_only=True)
    pic = dos.pic
    clock = lambda: cpu.instruction_count / (6428 * 70)  # noqa: E731
    dos.time_source = clock
    tick = {"next": clock()}

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

    s = cpu.s
    res = {"calls": 0, "ok": 0, "bad": 0}
    samples = []
    for _ in range(5_000_000):
        if cpu.instruction_count % 1500 == 0:
            pump()
        if s.cs == CS and s.ip == ENTRY:
            if True:                                            # the pass runs every gameplay frame
                saved_s, saved_mem, saved_ega = copy.deepcopy(s), bytearray(m.data), _ega(m)
                saved_gc = dict(getattr(dos, "_gc_regs", {}))
                # 1) ASM oracle -> run to the ret + execute it
                guard = 0
                while not (s.cs == CS and s.ip == EXIT):
                    interpret_current_instruction_without_hook(cpu)
                    guard += 1
                    if guard > 100000:
                        print("INCOMPLETE")
                        return 1
                interpret_current_instruction_without_hook(cpu)        # the ret
                asm_s, asm_mem, asm_ega = _regs(s), bytearray(m.data), _ega(m)
                # 2) restore + run the replacement
                cpu.s = copy.deepcopy(saved_s)
                m.data[:] = saved_mem
                for a, v in saved_ega.items():
                    setattr(m, a, v)
                if hasattr(dos, "_gc_regs"):
                    dos._gc_regs = dict(saved_gc)
                foreground_tiles(cpu)
                rec_s, rec_mem, rec_ega = _regs(cpu.s), m.data, _ega(m)
                # 3) compare whole machine
                mem_diff = [i for i in range(len(asm_mem)) if asm_mem[i] != rec_mem[i]]
                reg_diff = {r: (asm_s[r], rec_s[r]) for r in _REGS if asm_s[r] != rec_s[r]}
                ega_diff = {a: (asm_ega[a], rec_ega[a]) for a in _EGA if asm_ega[a] != rec_ega[a]}
                # The contract is the LIVE state: all memory at/above SP + the EGA registers. Below-SP
                # bytes (the ASM's push/pop scratch) are dead, and ax/bx/cx/dx/si/di/bp are clobbered
                # scratch the callers (024D/5100: an immediate CALL to 26FA) never read before reloading.
                sp_lin0 = (asm_s["ss"] << 4) + asm_s["sp"]
                live_mem = [d for d in mem_diff if d >= sp_lin0]
                _SCRATCH = {"ax", "bx", "cx", "dx", "si", "di", "bp", "flags"}
                res["calls"] += 1
                ok = not live_mem and not ega_diff and set(reg_diff).issubset(_SCRATCH)
                res["ok" if ok else "bad"] += 1
                if not ok and len(samples) < 8:
                    sp_lin = (asm_s["ss"] << 4) + asm_s["sp"]
                    live = [d for d in mem_diff if d >= sp_lin]
                    samples.append(dict(mem=len(mem_diff), live=len(live), regs=list(reg_diff),
                                        ega=ega_diff, first=hex(mem_diff[0]) if mem_diff else None))
                # 4) restore the ASM result to continue the demo
                cpu.s = copy.deepcopy(saved_s)
                for r in _REGS:
                    setattr(cpu.s, r, asm_s[r])
                m.data[:] = asm_mem
                if res["calls"] >= max_calls:
                    break
        cpu.step()

    print(f"FOREGROUND HOOK WHOLE-STATE: calls={res['calls']} ok={res['ok']} bad={res['bad']}")
    for i, sm in enumerate(samples):
        print(f"  [{i}] {sm}")
    return 0 if res["calls"] and res["bad"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
