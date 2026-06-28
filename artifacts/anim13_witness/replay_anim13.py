import argparse, sys, pickle
sys.path.insert(0, "."); sys.path.insert(0, "scripts")
from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.input_demo import InputDemoPlayback
from play import _make_replay_runtime
SCR=r"C:/Users/jiriv/AppData/Local/Temp/claude/C--claudework/1607d028-2657-4752-966a-aca707fe0acc/scratchpad"
dump=pickle.load(open(SCR+"/anim13_crash.pkl","rb"))
pb=InputDemoPlayback.load("artifacts/demo_pre2_20260627_120536")
args=argparse.Namespace(exe="assets/pre2.exe",game_root="assets",audio="off",fast_adlib=False,timer_irq=True,
    input_irq_steps=2_000_000,steps=None,chunk_steps=1250,present_hz=120,retrace_pulse=0.06,verify=False)
rt=_make_replay_runtime(args,pb); cpu=rt.cpu
cpu.replacement_hooks.clear(); cpu.hook_names.clear()   # pure ASM, no hooks
cpu.mem.data[:len(dump["mem"])]=dump["mem"]
for k,v in dump["regs"].items(): setattr(cpu.s, k, v)
print(f"restored at CS:IP={cpu.s.cs:04X}:{cpu.s.ip:04X}  (expect 1030:58A7)")
DS=(0x1A0F<<4)&0xFFFFF
def rb(o): return cpu.mem.data[(DS+o)&0xFFFFF]
def rw(o): return cpu.mem.data[(DS+o)&0xFFFFF]|(cpu.mem.data[(DS+o+1)&0xFFFFF]<<8)
# snapshot whole DS, run the FSM frame (58A7 until ip hits 5A0F = X-integrate), diff
before=bytes(cpu.mem.data[DS:DS+0x10000])
reached5d8a=[False]
guard=0
while not (cpu.s.cs==0x1030 and cpu.s.ip==0x5A0F):
    if cpu.s.cs==0x1030 and cpu.s.ip==0x5D8A: reached5d8a[0]=True
    interpret_current_instruction_without_hook(cpu)
    guard+=1
    if guard>50000: print("GUARD"); break
after=cpu.mem.data[DS:DS+0x10000]
print(f"reached 5D8A: {reached5d8a[0]}  instr={guard}")
diffs=[i for i in range(0x10000) if before[i]!=after[i]]
grp=[]
for i in diffs:
    if grp and i==grp[-1][1]+1: grp[-1]=(grp[-1][0],i)
    else: grp.append((i,i))
print(f"DS deltas: {len(diffs)} bytes, {len(grp)} regions")
for lo,hi in grp:
    vo=" ".join("%02x"%before[k] for k in range(lo,min(hi+1,lo+16)))
    vn=" ".join("%02x"%after[k] for k in range(lo,min(hi+1,lo+16)))
    print(f"  [{lo:#06x}..{hi:#06x}] ({hi-lo+1}b) {vo} -> {vn}")
