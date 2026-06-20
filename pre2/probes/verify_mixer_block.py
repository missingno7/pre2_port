"""TEMPORARY probe — in-VM lockstep verify of the recovered full-block mixer.

Cold-boots with the emulated SB; brackets the ISR mix section (``20AB``..``211C``):
captures the 4 channel states + their instruments, the SFX state, the volume table
and the music flag, runs the recovered ``mix_block`` on a fresh buffer, lets the ASM
run to the EOI, then diffs the 168-byte PCM block + the updated channel/SFX state.
This is the Layer-4 contract: same playback state + SFX -> same PCM buffer.

Retire when: a headless mix-block lockstep is folded into the test suite.
Run:  python -m pre2.probes.verify_mixer_block
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dos_re.interrupts import deliver_scancode
from dos_re.runtime import enable_sound_blaster
from pre2.bridge import audio as A
from pre2.recovered.mixer import mix_block
from pre2.runtime import create_pre2_runtime

MIX_SECTION = (0x1030, 0x20AB)   # start of the ISR's mix (after the DMA play)
MIX_EOI = 0x211C                 # the EOI right after the mix
LIMIT = 120


def main() -> int:
    rt = create_pre2_runtime(str(ROOT / "assets" / "pre2.exe"),
                             game_root=str(ROOT / "assets"), fast_adlib=True)
    cpu = rt.cpu
    cpu.trace_enabled = False
    sb = enable_sound_blaster(rt)
    pic = rt.dos.pic
    cpu.pending_irq = lambda: pic.acknowledge()

    state = {"n": 0}
    diverged: list[str] = []

    def _run_to(c, ip):
        fn = c.replacement_hooks.pop(MIX_SECTION, None)
        nm = c.hook_names.pop(MIX_SECTION, None)
        try:
            for _ in range(4_000_000):
                c.step()
                if (c.s.cs & 0xFFFF) == 0x1030 and (c.s.ip & 0xFFFF) == ip:
                    break
        finally:
            if fn is not None:
                c.replacement_hooks[MIX_SECTION] = fn
            if nm is not None:
                c.hook_names[MIX_SECTION] = nm

    def handler(c):
        mem = c.mem
        channels = [A.read_channel(mem, ch) for ch in range(A.NUM_CHANNELS)]
        instruments = [A.read_instrument(mem, channels[ch].instrument, channels[ch].end)
                       for ch in range(A.NUM_CHANNELS)]
        sfx = A.read_sfx(mem)
        vol = A.read_volume_table(mem)
        music = A.music_on(mem)
        buf_flat = A.fill_buffer_flat(mem)

        buf = bytearray(A.BLOCK_LEN)
        try:
            new_ch, new_sfx = mix_block(buf, channels, instruments, vol, sfx, music)
        except Exception as exc:  # noqa: BLE001
            diverged.append(f"recovered raised {type(exc).__name__}: {exc}")
            _run_to(c, MIX_EOI)
            return

        _run_to(c, MIX_EOI)

        reason = None
        asm_block = bytes(mem.data[buf_flat:buf_flat + A.BLOCK_LEN])
        if asm_block != bytes(buf):
            i = next(k for k in range(A.BLOCK_LEN) if asm_block[k] != buf[k])
            reason = f"block @{i}: asm={asm_block[i]:02X} rec={buf[i]:02X}"
        if reason is None:
            for ch in range(A.NUM_CHANNELS):
                a = A.read_channel(mem, ch)
                r = new_ch[ch]
                if (a.pos, a.end, a.frac) != (r.pos, r.end, r.frac):
                    reason = f"ch{ch} state asm=({a.pos:04X},{a.end:04X},{a.frac:02X}) rec=({r.pos:04X},{r.end:04X},{r.frac:02X})"
                    break
        if reason is None:
            a = A.read_sfx(mem)
            if (a.pos, a.remaining) != (new_sfx.pos, new_sfx.remaining):
                reason = f"sfx asm=({a.pos:04X},{a.remaining}) rec=({new_sfx.pos:04X},{new_sfx.remaining})"

        state["n"] += 1
        if reason is not None:
            diverged.append(f"block#{state['n']} music={music} sfx_rem={sfx.remaining}: {reason}")
        if state["n"] >= LIMIT or diverged:
            cpu.replacement_hooks.pop(MIX_SECTION, None)
            cpu.hook_names.pop(MIX_SECTION, None)

    cpu.replacement_hooks[MIX_SECTION] = handler
    cpu.hook_names[MIX_SECTION] = "mix_block_verify"

    held = [False]
    for f in range(1400):
        pic.raise_irq(0)
        try:
            for _ in range(4000):
                cpu.step()
        except Exception as exc:  # noqa: BLE001
            print(f"stopped frame {f}: {type(exc).__name__}: {exc}")
            break
        if MIX_SECTION not in cpu.replacement_hooks:
            break
        if f > 40:
            want = (f % 120) < 50
            if want and not held[0]:
                deliver_scancode(rt, 0x1C, max_steps=100000); held[0] = True
            elif not want and held[0]:
                deliver_scancode(rt, 0x9C, max_steps=100000); held[0] = False

    print(f"mix_block verified={state['n']}")
    print(f"divergences={diverged[:6]}")
    ok = not diverged and state["n"] > 0
    print("MIX-BLOCK LOCKSTEP:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
