import argparse, sys
sys.path.insert(0, "."); sys.path.insert(0, "scripts")
from pathlib import Path
from capstone import Cs, CS_ARCH_X86, CS_MODE_16
from dos_re.input_demo import InputDemoPlayback
from play import _make_replay_runtime
pb = InputDemoPlayback.load("artifacts/demo_pre2_20260627_120536")
args = argparse.Namespace(exe="assets/pre2.exe", game_root="assets", audio="off", fast_adlib=False,
    timer_irq=True, input_irq_steps=2_000_000, steps=None, chunk_steps=1250, present_hz=120, retrace_pulse=0.06, verify=False)
rt = _make_replay_runtime(args, pb); cpu = rt.cpu
base = 0x1030 << 4; md = Cs(CS_ARCH_X86, CS_MODE_16)
print("===== 350c full =====")
for ins in md.disasm(bytes(cpu.mem.data[base+0x350C:base+0x3588]),0x350C):
    print(f"  {ins.address:04X}: {ins.mnemonic:7s} {ins.op_str}")
    if ins.mnemonic in ("ret","retf"): break
