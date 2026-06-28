"""Build a fixed anim13 oracle from the crash dump: full memory BEFORE (58A7 entry) and AFTER (5A0F),
so the recovery can be iterated offline without re-running the ASM each time."""
import argparse, sys, pickle
sys.path.insert(0, "."); sys.path.insert(0, "scripts")
from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.input_demo import InputDemoPlayback
from play import _make_replay_runtime
WSCR=r"C:/Users/jiriv/AppData/Local/Temp/claude/C--claudework/1607d028-2657-4752-966a-aca707fe0acc/scratchpad"
dump=pickle.load(open(WSCR+"/anim13_crash.pkl","rb"))
pb=InputDemoPlayback.load("artifacts/demo_pre2_20260627_120536")
args=argparse.Namespace(exe="assets/pre2.exe",game_root="assets",audio="off",fast_adlib=False,timer_irq=True,
    input_irq_steps=2_000_000,steps=None,chunk_steps=1250,present_hz=120,retrace_pulse=0.06,verify=False)
rt=_make_replay_runtime(args,pb); cpu=rt.cpu
cpu.replacement_hooks.clear(); cpu.hook_names.clear()
cpu.mem.data[:len(dump["mem"])]=dump["mem"]
for k,v in dump["regs"].items(): setattr(cpu.s,k,v)
assert cpu.s.cs==0x1030 and cpu.s.ip==0x58A7, (cpu.s.cs,cpu.s.ip)
before=bytes(cpu.mem.data)       # full 1MB before
g=0
while not (cpu.s.cs==0x1030 and cpu.s.ip==0x5A0F):
    interpret_current_instruction_without_hook(cpu); g+=1
    if g>50000: raise SystemExit("guard")
after=bytes(cpu.mem.data)        # full 1MB after
pickle.dump({"before":before,"after":after,"regs":dict(dump["regs"])}, open(WSCR+"/anim13_oracle.pkl","wb"))
# report all changed regions (DS 0x1A0F + planes 0xA000)
diffs=[i for i in range(len(before)) if before[i]!=after[i]]
grp=[]
for i in diffs:
    if grp and i==grp[-1][1]+1: grp[-1]=(grp[-1][0],i)
    else: grp.append((i,i))
def seg(a):
    if 0xA0000<=a<0xB0000: return f"A000:{a-0xA0000:#06x}"
    if 0x1A0F0<=a<0x2A0F0: return f"1A0F:{a-0x1A0F0:#06x}"
    return f"{a:#07x}"
print(f"anim13 oracle built: {len(diffs)} bytes changed in {len(grp)} regions, {g} instr")
for lo,hi in grp: print(f"  [{seg(lo)}..{seg(hi)}] ({hi-lo+1}b)")
