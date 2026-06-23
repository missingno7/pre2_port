"""Throwaway witness: per-call whole-state lockstep for the wired draw_string (1030:9886).

At every draw_string entry during the menu-navigating demo replay: snapshot the entry
state, drive the real ASM to the RET (98FF) and past it, then restore the entry state and
run the live checkpoint hook from the same point — and compare the WHOLE machine (all
memory + all registers). This proves the live replacement's contract is complete, not just
the planes-2|3 + pen the fast verifier checks. (The menu is reached only by replaying
demo_pre2_20260622_192206; draw_string never fires in steady gameplay.)
"""
from __future__ import annotations

import copy
from pathlib import Path

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.input_demo import InputDemoPlayback
from dos_re.interrupts import deliver_scancode
from pre2.checkpoints.text import draw_string_hook
from pre2.runtime import load_pre2_snapshot

ROOT = Path(__file__).resolve().parents[2]
CS, ENTRY, EXIT = 0x1030, 0x9886, 0x98FF
DEMO = ROOT / "artifacts" / "demo_pre2_20260622_192206"
_REGS = ("ax", "bx", "cx", "dx", "si", "di", "bp", "sp", "ds", "es", "ss", "cs", "ip", "flags")


def _regs(s):
    return {r: getattr(s, r) for r in _REGS}


def main(max_calls=40, max_frames=4000):
    pb = InputDemoPlayback.load(DEMO)
    meta = pb.manifest.get("metadata", {})
    chunk = int(meta.get("chunk_steps", 4000))
    rt = load_pre2_snapshot(ROOT / "assets" / "pre2.exe", pb.snapshot_path(),
                            game_root=ROOT / "assets", fast_adlib=bool(meta.get("fast_adlib", False)))
    cpu = rt.cpu
    cpu.trace_enabled = False
    res = {"calls": 0, "ok": 0, "bad": 0}
    samples = []

    def probe(c):
        s = c.s
        saved_s = copy.deepcopy(s)
        saved_mem = bytearray(c.mem.data)
        # 1) ASM oracle: drive 9886 -> 98FF, then execute the ret
        steps = 0
        while not (s.cs == CS and s.ip == EXIT):
            interpret_current_instruction_without_hook(c)
            steps += 1
            if steps > 200_000:
                res["bad"] += 1
                samples.append(("INCOMPLETE",))
                return
        interpret_current_instruction_without_hook(c)          # the ret
        asm_s, asm_mem = _regs(s), bytearray(c.mem.data)
        # 2) restore entry state, run the live hook
        c.s = copy.deepcopy(saved_s)
        c.mem.data[:] = saved_mem
        draw_string_hook(c)                                    # non-verify -> draws + sets bx/ds/ip + ret
        rec_s, rec_mem = _regs(c.s), c.mem.data
        # 3) compare whole machine
        mem_diff = [i for i in range(len(asm_mem)) if asm_mem[i] != rec_mem[i]]
        reg_diff = {r: (asm_s[r], rec_s[r]) for r in _REGS if asm_s[r] != rec_s[r]}
        res["calls"] += 1
        ok = not mem_diff and not reg_diff
        res["ok" if ok else "bad"] += 1
        if not ok and len(samples) < 8:
            sp_lin = (asm_s["ss"] << 4) + asm_s["sp"]
            live_mem = [d for d in mem_diff if d >= sp_lin]   # at/above SP = live stack/data
            samples.append(dict(mem_diff=len(mem_diff), live_mem=len(live_mem),
                                regs=list(reg_diff), first_mem=hex(mem_diff[0]) if mem_diff else None,
                                sp=hex(sp_lin)))
        # 4) restore ASM result so the demo continues correctly
        c.s = copy.deepcopy(saved_s)
        for r in _REGS:
            setattr(c.s, r, asm_s[r])
        c.mem.data[:] = asm_mem

    cpu.replacement_hooks[(CS, ENTRY)] = probe
    cpu.hook_names[(CS, ENTRY)] = "probe:draw_string_wholestate"

    for f in range(max_frames):
        try:
            pb.apply_to_runtime(f, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=2000))
            for _ in range(chunk):
                cpu.step()
        except Exception as exc:  # noqa: BLE001
            print(f"stopped frame {f}: {type(exc).__name__}: {exc}")
            break
        if res["calls"] >= max_calls:
            break

    print(f"DRAW_STRING WHOLE-STATE: calls={res['calls']} ok={res['ok']} bad={res['bad']}")
    for i, sm in enumerate(samples):
        print(f"  [{i}] {sm}")
    return res


if __name__ == "__main__":
    main()
