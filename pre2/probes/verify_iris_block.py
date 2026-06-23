"""Throwaway witness: prove the recovered iris *block* (build->write tables->draw)
reproduces the live ASM planes byte-exact, frame by frame.

The unit tests (tests/test_transition.py) prove each primitive in isolation against a
golden. This proves their COMPOSITION over the real per-frame block 1030:31F4..32B0
against the live ASM: hook 31F4, snapshot the inputs + before-planes, run the recovered
build_scaled_columns + draw_scale_frame on a copy, drive the real ASM to 32B0, and diff
the four EGA planes. Snapshot 074654 is paused inside the iris and loops back to 31F4
each frame, so stepping forward captures the remaining frames until the radius hits 0.
"""
from __future__ import annotations

from pathlib import Path

from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from pre2.bridge.object_render import read_planes
from pre2.recovered.transition import build_scaled_columns, draw_scale_frame
from pre2.runtime import load_pre2_snapshot

ROOT = Path(__file__).resolve().parents[2]
CS = 0x1030
ENTRY, EXIT = 0x31F4, 0x32B0
_DS = 0x1A0F
# DGROUP offsets (from the disasm of 31F4..32B0)
RADIUS, X_OFF, Y_OFF, X_CLAMP, PAGE = 0x2DD0, 0x2DC6, 0x2DC8, 0x2DC4, 0x2DD8
COS_T, SIN_T = 0x7090, 0x6F90          # quarter-circle cos/sin tables
TBL_X, TBL_Y = 0x6B14, 0x6A88          # scaled-column tables
COLS = 0x41


def _rb(mem, off):
    return mem.data[(_DS << 4) + off]


def _rw(mem, off):
    b = (_DS << 4) + off
    return mem.data[b] | (mem.data[b + 1] << 8)


def _rwsigned(mem, off):
    v = _rw(mem, off)
    return v - 0x10000 if v & 0x8000 else v


def main(snapshot="artifacts/snapshot_pre2_iris_20260623_074654", max_steps=4_000_000):
    rt = load_pre2_snapshot(ROOT / "assets" / "pre2.exe", ROOT / snapshot,
                            game_root=ROOT / "assets", native_replacements=True)
    cpu = rt.cpu
    cpu.trace_enabled = False
    s = cpu.s
    res = {"frames": 0, "ok": 0, "bad": 0}
    samples = []

    def on_iris(c):
        mem = c.mem
        scale = _rb(mem, RADIUS)
        x_off = _rwsigned(mem, X_OFF)
        y_off = _rwsigned(mem, Y_OFF)
        x_clamp = _rwsigned(mem, X_CLAMP)
        page = _rw(mem, PAGE)
        src_x = list(mem.data[(_DS << 4) + COS_T:(_DS << 4) + COS_T + COLS])
        src_y = list(mem.data[(_DS << 4) + SIN_T:(_DS << 4) + SIN_T + COLS])
        # current (stale) word tables — build overwrites the first `count`
        tbl_x = [_rwsigned(mem, TBL_X + 2 * i) for i in range(COLS)]
        tbl_y = [_rwsigned(mem, TBL_Y + 2 * i) for i in range(COLS)]
        before = read_planes(mem)

        # recovered block on a copy
        xs, ys = build_scaled_columns(src_x, src_y, scale, x_off, y_off, x_clamp)
        for i, v in enumerate(xs):
            tbl_x[i] = v
        for i, v in enumerate(ys):
            tbl_y[i] = v
        rec = [bytearray(p) for p in before]
        draw_scale_frame(rec, tbl_x, tbl_y, len(xs), x_off, y_off, x_clamp, page)

        # drive the real ASM through the block to 32B0
        steps = 0
        while not (s.cs == CS and s.ip == EXIT):
            interpret_current_instruction_without_hook(c)
            steps += 1
            if steps > 500_000:
                res["bad"] += 1
                samples.append(("INCOMPLETE", scale))
                return
        after = read_planes(mem)

        ok = all(rec[p] == after[p] for p in range(4))
        res["frames"] += 1
        res["ok" if ok else "bad"] += 1
        if not ok or len(samples) < 4:
            diffs = [p for p in range(4) if rec[p] != after[p]]
            samples.append(dict(scale=scale, ncols=len(xs), x_off=x_off, y_off=y_off,
                                page=hex(page), ok=ok, diff_planes=diffs))

    cpu.replacement_hooks[(CS, ENTRY)] = on_iris
    cpu.hook_names[(CS, ENTRY)] = "probe:iris_block"

    for _ in range(max_steps):
        try:
            cpu.step()
        except Exception as exc:  # noqa: BLE001
            print(f"stopped: {type(exc).__name__}: {exc}")
            break
        if res["frames"] >= 60:
            break

    print(f"IRIS BLOCK: frames={res['frames']} ok={res['ok']} bad={res['bad']}")
    for i, sm in enumerate(samples):
        print(f"  [{i}] {sm}")
    return res


if __name__ == "__main__":
    main()
