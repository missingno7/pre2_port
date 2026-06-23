"""Throwaway witness: per-call whole-state lockstep for the wired scroll_blit (1030:965A..969C)
on the map-scroll snapshot. At each block entry: snapshot regs+mem, drive the ASM to the exit
(969C), restore, run the live checkpoint hook from the same entry, diff WHOLE memory + registers."""
from __future__ import annotations
from pathlib import Path
from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from pre2.checkpoints.present import scroll_blit
from pre2.runtime import load_pre2_snapshot
ROOT = Path(__file__).resolve().parents[2]
CS, ENTRY, EXIT = 0x1030, 0x965A, 0x969C
SNAP = "artifacts/snapshot_pre2_mapscroll_20260623_110253"
_REGS = ("ax","bx","cx","dx","si","di","bp","sp","ds","es","ss","cs","ip","flags")
def _regs(s): return {r: getattr(s, r) for r in _REGS}
def _set(s, d):
    for r in _REGS: setattr(s, r, d[r])
def main(max_calls=60):
    rt = load_pre2_snapshot(ROOT/"assets"/"pre2.exe", ROOT/SNAP, game_root=ROOT/"assets", native_replacements=True)
    cpu = rt.cpu; cpu.trace_enabled = False
    res = {"calls":0,"ok":0,"bad":0,"asm_instr":0}; samples=[]
    def probe(c):
        entry = _regs(c.s); saved_mem = bytearray(c.mem.data)
        steps=0
        while not (c.s.cs==CS and c.s.ip==EXIT):
            interpret_current_instruction_without_hook(c); steps+=1
            if steps>500000: res["bad"]+=1; return
        asm_s, asm_mem = _regs(c.s), bytearray(c.mem.data)
        _set(c.s, entry); c.mem.data[:] = saved_mem      # restore entry, in place
        scroll_blit(c)                                   # live hook
        rec_s, rec_mem = _regs(c.s), c.mem.data
        mem_diff = [i for i in range(len(asm_mem)) if asm_mem[i]!=rec_mem[i]]
        reg_diff = {r:(asm_s[r],rec_s[r]) for r in _REGS if asm_s[r]!=rec_s[r]}
        res["calls"]+=1; res["asm_instr"]+=steps
        ok = not mem_diff and not reg_diff
        res["ok" if ok else "bad"]+=1
        if not ok and len(samples)<6:
            sp=(asm_s["ss"]<<4)+asm_s["sp"]
            samples.append(dict(asm_instr=steps, mem_diff=len(mem_diff),
                                live_mem=len([d for d in mem_diff if d>=sp]),
                                regs=list(reg_diff)))
        _set(c.s, asm_s); c.mem.data[:] = asm_mem        # continue with ASM result
    cpu.replacement_hooks[(CS,ENTRY)] = probe
    cpu.hook_names[(CS,ENTRY)] = "probe:scroll_blit_ws"
    for _ in range(8_000_000):
        try: cpu.step()
        except Exception as e: print("stop",type(e).__name__,e); break
        if res["calls"]>=max_calls: break
    avg = res["asm_instr"]//max(res["calls"],1)
    print(f"SCROLL_BLIT WHOLE-STATE: calls={res['calls']} ok={res['ok']} bad={res['bad']}  ASM block ~{avg} instr/call (native = 1)")
    for i,sm in enumerate(samples): print(f"  [{i}] {sm}")
    return res
if __name__=="__main__": main()
