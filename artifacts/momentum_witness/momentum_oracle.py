import argparse, sys, pickle
sys.path.insert(0,"."); sys.path.insert(0,"scripts")
from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.input_demo import InputDemoPlayback
from play import _make_replay_runtime
WSCR=r"C:/Users/jiriv/AppData/Local/Temp/claude/C--claudework/1607d028-2657-4752-966a-aca707fe0acc/scratchpad"
dump=pickle.load(open(WSCR+"/momentum_crash.pkl","rb"))
pb=InputDemoPlayback.load("artifacts/demo_pre2_20260626_105310")
args=argparse.Namespace(exe="assets/pre2.exe",game_root="assets",audio="off",fast_adlib=False,timer_irq=True,
  input_irq_steps=2_000_000,steps=None,chunk_steps=1250,present_hz=120,retrace_pulse=0.06,verify=False)
rt=_make_replay_runtime(args,pb); cpu=rt.cpu
cpu.replacement_hooks.clear(); cpu.hook_names.clear()
cpu.mem.data[:len(dump["mem"])]=dump["mem"]
for k,v in dump["regs"].items(): setattr(cpu.s,k,v)
assert cpu.s.cs==0x1030 and cpu.s.ip==0x58A7
DS=0x1A0F<<4
def rb(b,a): return b[DS+a]
def rw(b,a): return b[DS+a]|(b[DS+a+1]<<8)
before=bytes(cpu.mem.data)
# trace whether the momentum path reaches 5A0B (dispatch) or 5A0F (skip)
reached=[]
g=0
while not (cpu.s.cs==0x1030 and cpu.s.ip==0x5A0F):
    if cpu.s.cs==0x1030 and cpu.s.ip in (0x596A,0x5A0B): reached.append(hex(cpu.s.ip))
    interpret_current_instruction_without_hook(cpu); g+=1
    if g>50000: raise SystemExit("guard")
after=bytes(cpu.mem.data)
pickle.dump({"before":before,"after":after,"regs":dict(dump["regs"])},open(WSCR+"/momentum_oracle.pkl","wb"))
print("entry state: [6bc5]=%#x [6bc6]=%#x [6bc7]=%#x [7b1a]=%#x Yvel=%#x Xvel=%#x [27ea]=%#x [27eb]=%#x"%(
  rb(before,0x6bc5),rb(before,0x6bc6),rb(before,0x6bc7),rb(before,0x7b1a),rw(before,0x4f2a),rw(before,0x4f22),rb(before,0x27ea),rb(before,0x27eb)))
print("path:",reached,"instr=",g)
diffs=[i for i in range(len(before)) if before[i]!=after[i]]
# only DS-segment + a few flags (momentum path is pure-state, no planes expected)
ds_d=[(i-DS) for i in diffs if DS<=i<DS+0x10000]
oth=[i for i in diffs if not (DS<=i<DS+0x10000)]
print(f"DS deltas ({len(ds_d)}):")
for a in ds_d: print(f"  [1A0F:{a:#06x}] {before[DS+a]:#04x} -> {after[DS+a]:#04x}")
print(f"non-DS changed regions: {len(oth)} bytes")
