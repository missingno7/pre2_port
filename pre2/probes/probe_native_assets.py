"""Verify the native asset loader (load_sqz) — the standalone 107B replacement.

Load real .SQZ game files into a NativeGameState's stacking buffer and assert the loader's contract: the decoded
bytes land at the load pointer [0x2875], the pointer is bumped by the asset's paragraph count, and the next asset
stacks immediately after. The decode itself is the already-ASM-verified unpack_sqz; this checks the file read +
the load-pointer bookkeeping the original allocator does.

Run: python -m pre2.probes.probe_native_assets
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from pre2.codecs.sqz import sqz_bump_advance, unpack_sqz
from pre2.native.assets import LOAD_PTR, load_sqz
from pre2.native.state import DATA_SEG, NativeGameState

_DS = DATA_SEG << 4
_GR = str(ROOT / "assets")


def main():
    state = NativeGameState(bytearray(0x100000))
    state.data[_DS + LOAD_PTR] = 0x00
    state.data[_DS + LOAD_PTR + 1] = 0x40                     # [0x2875] = 0x4000 (a free load base)

    raw1 = (ROOT / "assets/LEVEL2.SQZ").read_bytes()
    exp1 = unpack_sqz(raw1)
    seg1 = load_sqz(state, "LEVEL2.SQZ", game_root=_GR)
    data1 = bytes(state.data[seg1 << 4: (seg1 << 4) + len(exp1)])
    ptr1 = state.rw(LOAD_PTR)
    ok_land = seg1 == 0x4000 and data1 == exp1
    ok_bump = ptr1 == (0x4000 + sqz_bump_advance(raw1)) & 0xFFFF

    # second asset stacks at the bumped pointer
    seg2 = load_sqz(state, "BACK1.SQZ", game_root=_GR)
    raw2 = (ROOT / "assets/BACK1.SQZ").read_bytes()
    data2 = bytes(state.data[seg2 << 4: (seg2 << 4) + len(unpack_sqz(raw2))])
    ok_stack = seg2 == ptr1 and data2 == unpack_sqz(raw2)

    print(f"LEVEL2.SQZ -> seg {seg1:#06x}, {len(exp1)} bytes; land={ok_land} bump={ok_bump} "
          f"(ptr {0x4000:#06x}->{ptr1:#06x})")
    print(f"BACK1.SQZ stacks at seg {seg2:#06x}: {ok_stack}")
    ok = ok_land and ok_bump and ok_stack
    print("native asset loader:", "PASS (loads + stacks .SQZ from game files)" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
