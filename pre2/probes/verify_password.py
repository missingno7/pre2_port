"""Verify the recovered password generator (pre2.recovered.password.level_code) by INVOKING the original ASM
routine 1030:932F in the VM for every level index and comparing. Run: python pre2/probes/verify_password.py
"""
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

from pre2.recovered.password import DEFAULT_ROT, DEFAULT_SEED, level_code, password_table
from pre2.runtime import load_pre2_snapshot

_SENTINEL = 0xFFF0   # a return address we detect when 932F's RET pops it


def call_932f(rt, index):
    """Invoke 1030:932F(ax=index) in the VM and return ax (the 16-bit code)."""
    cpu = rt.cpu
    s = cpu.s
    save = s.snapshot() if hasattr(s, "snapshot") else None
    s.ax = index & 0xFFFF
    s.sp = (s.sp - 2) & 0xFFFF                       # push the sentinel return address
    base = (s.ss << 4) + s.sp
    cpu.mem.data[base] = _SENTINEL & 0xFF
    cpu.mem.data[base + 1] = (_SENTINEL >> 8) & 0xFF
    s.cs, s.ip = 0x1030, 0x932F
    for _ in range(200000):
        cpu.step()
        if (s.cs & 0xFFFF) == 0x1030 and (s.ip & 0xFFFF) == _SENTINEL:
            break
    return s.ax & 0xFFFF


def main():
    rt = load_pre2_snapshot("assets/pre2.exe", "artifacts/snapshot_pre2_20260625_170717",
                            game_root="assets", native_replacements=True)
    d = rt.cpu.mem.data
    seed = d[(0x1A0F << 4) + 0xA333] | (d[(0x1A0F << 4) + 0xA334] << 8)
    rot = d[(0x1030 << 4) + 5]
    print(f"  build seed [0xA333]={seed:#06x}  rot cs:[5]={rot}  (recovered defaults {DEFAULT_SEED:#04x}/{DEFAULT_ROT})")
    ok = True
    for idx in range(0x13):                          # the validator's level loop range 0..0x12
        asm = call_932f(rt, idx)
        rec = level_code(idx, seed, rot)
        same = asm == rec
        ok = ok and same
        if not same or idx in (0, 1, 10):
            print(f"    idx {idx:2d}: asm={asm:04X} recovered={rec:04X} {'OK' if same else 'MISMATCH'}")
    print("  password table (this build):")
    for lvl, beg, exp in password_table(seed, rot):
        print(f"    L{lvl:<2}  beginner={beg}  expert={exp}")
    print("RECOVERED PASSWORD GENERATOR vs ASM 932F:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
