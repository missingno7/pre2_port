"""TEMPORARY probe — in-VM lockstep verify of the recovered per-channel mixer (218F).

Cold-boots with the emulated SB so the original driver streams audio; at each
``1030:218F`` call (per MOD channel, in the block-refill ISR) it captures the
channel/instrument/volume state + the partially-mixed block, runs the recovered
``mix_channel`` on a copy, lets the ASM run to its RET, then diffs the 168-byte
block and the updated channel state (pos/end/frac). Zero divergence.

Retire when: a headless 218F lockstep is folded into the test suite.
Run:  python -m pre2.probes.verify_mixer
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dos_re.interrupts import deliver_scancode
from dos_re.runtime import enable_sound_blaster
from pre2.bridge import audio as A
from pre2.recovered.mixer import mix_channel
from pre2.runtime import create_pre2_runtime

MIX = (0x1030, 0x218F)
LIMIT = 200


def main() -> int:
    rt = create_pre2_runtime(str(ROOT / "assets" / "pre2.exe"),
                             game_root=str(ROOT / "assets"), fast_adlib=True)
    cpu = rt.cpu
    cpu.trace_enabled = False
    sb = enable_sound_blaster(rt)
    pic = rt.dos.pic
    cpu.pending_irq = lambda: pic.acknowledge()

    state = {"active": 0, "off": 0}
    diverged: list[str] = []

    def _run_to_return(c):
        # 218F abuses SP as a scratch register (sp = sample end pointer), so an
        # sp-based return check fires mid-loop. Detect the return by the pushed
        # return address instead (read it before the routine clobbers sp).
        entry_cs = c.s.cs & 0xFFFF
        ret_ip = c.mem.rw(c.s.ss & 0xFFFF, c.s.sp & 0xFFFF)
        fn = c.replacement_hooks.pop(MIX, None)
        nm = c.hook_names.pop(MIX, None)
        try:
            for _ in range(2_000_000):
                c.step()
                if (c.s.cs & 0xFFFF) == entry_cs and (c.s.ip & 0xFFFF) == ret_ip:
                    break
        finally:
            if fn is not None:
                c.replacement_hooks[MIX] = fn
            if nm is not None:
                c.hook_names[MIX] = nm

    def handler(c):
        mem = c.mem
        ch = (c.s.bx & 0xFFFF) >> 1
        cs = A.read_channel(mem, ch)
        instr = A.read_instrument(mem, cs.instrument, cs.end)
        vol = A.read_volume_table(mem)
        buf_flat = A.fill_buffer_flat(mem)
        snap = bytearray(mem.data[buf_flat:buf_flat + A.BLOCK_LEN])

        try:
            new_cs = mix_channel(snap, cs, instr, vol)
        except Exception as exc:  # noqa: BLE001
            diverged.append(f"recovered raised {type(exc).__name__}: {exc}")
            _run_to_return(c)
            return

        _run_to_return(c)

        reason = None
        asm_block = bytes(mem.data[buf_flat:buf_flat + A.BLOCK_LEN])
        if asm_block != bytes(snap):
            i = next(k for k in range(A.BLOCK_LEN) if asm_block[k] != snap[k])
            reason = f"block @{i}: asm={asm_block[i]:02X} rec={snap[i]:02X}"
        if reason is None:
            asm_cs = A.read_channel(mem, ch)
            if (asm_cs.pos, asm_cs.end, asm_cs.frac) != (new_cs.pos, new_cs.end, new_cs.frac):
                reason = f"state asm=({asm_cs.pos:04X},{asm_cs.end:04X},{asm_cs.frac:02X}) " \
                         f"rec=({new_cs.pos:04X},{new_cs.end:04X},{new_cs.frac:02X})"

        if cs.active:
            state["active"] += 1
        else:
            state["off"] += 1
        if reason is not None:
            diverged.append(f"ch{ch} pos={cs.pos:04X} instr={cs.instrument} vol={cs.volume} per={cs.period:04X}: {reason}")
        if state["active"] >= LIMIT or diverged:
            cpu.replacement_hooks.pop(MIX, None)
            cpu.hook_names.pop(MIX, None)

    cpu.replacement_hooks[MIX] = handler
    cpu.hook_names[MIX] = "mix_channel_verify"

    held = [False]
    for f in range(1400):
        pic.raise_irq(0)
        try:
            for _ in range(4000):
                cpu.step()
        except Exception as exc:  # noqa: BLE001
            print(f"stopped frame {f}: {type(exc).__name__}: {exc}")
            break
        if MIX not in cpu.replacement_hooks:
            break
        if f > 40:
            want = (f % 120) < 50
            if want and not held[0]:
                deliver_scancode(rt, 0x1C, max_steps=100000); held[0] = True
            elif not want and held[0]:
                deliver_scancode(rt, 0x9C, max_steps=100000); held[0] = False

    print(f"mix_channel verified: active={state['active']} off(silent)={state['off']}")
    print(f"divergences={diverged[:6]}")
    ok = not diverged and state["active"] > 0
    print("MIX-CHANNEL LOCKSTEP:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
