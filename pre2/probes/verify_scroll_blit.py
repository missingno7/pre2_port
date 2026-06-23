"""Throwaway witness: lockstep the recovered background scroll-blit (1030:965A..969C).

Hooks the mode-select shift-copy: captures scroll_x ([0xB19D], pre-increment) + the master
pattern segment ([0x2875]) + the planes before, drives the real ASM through the blit to
969C, then runs the recovered scroll_blit_column on the before-planes and diffs all 4 EGA
planes. Reached by replaying the menu demo (the scroll runs while [0xB19D] climbs to 0x280).
"""
from __future__ import annotations

from pathlib import Path

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.input_demo import InputDemoPlayback
from dos_re.interrupts import deliver_scancode
from pre2.bridge.object_render import read_planes
from pre2.recovered.present import scroll_blit_column
from pre2.runtime import load_pre2_snapshot

ROOT = Path(__file__).resolve().parents[2]
CS, ENTRY, AFTER = 0x1030, 0x965A, 0x969C
_DS = 0x1A0F
SCROLL_X, FONT_SEG = 0xB19D, 0x2875
DEMO = ROOT / "artifacts" / "demo_pre2_20260622_192206"


def _rw(mem, off):
    b = (_DS << 4) + off
    return mem.data[b] | (mem.data[b + 1] << 8)


def main(max_calls=80, max_frames=4000):
    pb = InputDemoPlayback.load(DEMO)
    meta = pb.manifest.get("metadata", {})
    chunk = int(meta.get("chunk_steps", 4000))
    rt = load_pre2_snapshot(ROOT / "assets" / "pre2.exe", pb.snapshot_path(),
                            game_root=ROOT / "assets", fast_adlib=bool(meta.get("fast_adlib", False)))
    cpu = rt.cpu
    cpu.trace_enabled = False
    s = cpu.s
    res = {"blits": 0, "skips": 0, "ok": 0, "bad": 0}
    samples = []

    def probe(c):
        mem = c.mem
        sx = _rw(mem, SCROLL_X)
        fseg = _rw(mem, FONT_SEG)
        fbase = (fseg << 4) & 0xFFFFF
        source = bytes(mem.data[fbase:fbase + 0x10000])
        before = read_planes(mem)
        steps = 0
        while not (s.cs == CS and s.ip == AFTER):
            interpret_current_instruction_without_hook(c)
            steps += 1
            if steps > 200_000:
                res["bad"] += 1
                return
        after = read_planes(mem)
        rec = [bytearray(p) for p in before]
        scroll_blit_column(rec, source, sx)
        ok = all(rec[p] == after[p] for p in range(4))
        if sx & 7:
            res["skips"] += 1
        else:
            res["blits"] += 1
        res["ok" if ok else "bad"] += 1
        if not ok and len(samples) < 6:
            for p in range(4):
                if rec[p] != after[p]:
                    i = next(k for k in range(len(after[p])) if after[p][k] != rec[p][k])
                    samples.append(dict(sx=hex(sx), plane=p, off=hex(i),
                                        asm=after[p][i], rec=rec[p][i],
                                        changed=sum(1 for k in range(0x2000) if after[p][k] != before[p][k])))
                    break

    cpu.replacement_hooks[(CS, ENTRY)] = probe
    cpu.hook_names[(CS, ENTRY)] = "probe:scroll_blit"

    for f in range(max_frames):
        try:
            pb.apply_to_runtime(f, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=2000))
            for _ in range(chunk):
                cpu.step()
        except Exception as exc:  # noqa: BLE001
            print(f"stopped frame {f}: {type(exc).__name__}: {exc}")
            break
        if res["blits"] >= max_calls:
            break

    print(f"SCROLL-BLIT: blits={res['blits']} skips={res['skips']} ok={res['ok']} bad={res['bad']}")
    for i, sm in enumerate(samples):
        print(f"  [{i}] {sm}")
    return res


if __name__ == "__main__":
    main()
