"""Diagnose enemy interpolation: dump the per-source-frame sprite list (id/flip/screen pos) so we can see
whether the back-and-forth enemy keeps a stable base_id, whether it flips, whether base_ids are duplicated
(queue mis-pairing risk), and whether its screen_x moves smoothly or jumps (e.g. at a direction flip)."""
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

from collections import Counter

from dos_re.cpu import IF
from dos_re.interrupts import deliver_interrupt
from dos_re.runtime import enable_sound_blaster
from pre2.bridge.render_state import read_renderer_state
from pre2.recovered.render_snapshot import build_frame_snapshot
from pre2.runtime import load_pre2_snapshot

CS = 0x1030
_6772 = (0x1030, 0x6772)


def run(snap, frames=30):
    rt = load_pre2_snapshot("assets/pre2.exe", snap, game_root="assets", native_replacements=True)
    cpu, dos = rt.cpu, rt.dos
    cpu.trace_enabled = False
    sb = enable_sound_blaster(rt, detection_only=True)
    pic = dos.pic
    det_speed = 6428 * 70
    dos.time_source = lambda: cpu.instruction_count / det_speed
    dos.vga_retrace_active_fraction = 0.06
    tick = {"next": 0.0}
    snaps = []
    orig = cpu.replacement_hooks.get(_6772)
    from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook

    def on_commit(c):
        try:
            gs = build_frame_snapshot(read_renderer_state(c.mem, dos, game_root="assets"))
            rows = [(s.base_id, s.sprite_id, bool(s.flip), s.screen_x, s.screen_y, s.is_hud,
                     int(s.mode), s.world_x, s.world_y) for s in gs.sprites]
            snaps.append((cpu.instruction_count, gs.camera.x_px, rows))
        except Exception as e:
            snaps.append((cpu.instruction_count, None, []))
        return orig(c) if orig is not None else interpret_current_instruction_without_hook(c)
    cpu.replacement_hooks[_6772] = on_commit

    def pump():
        now = cpu.instruction_count / det_speed
        tp = 1.0 / max(1.0, dos.pit_channel0_hz())
        while now >= tick["next"]:
            pic.raise_irq(0); tick["next"] += tp
            if tick["next"] < now - 0.25:
                tick["next"] = now + tp
        sb.service()
        g = 0
        while cpu.get_flag(IF) and g < 64:
            n = pic.acknowledge()
            if n is None:
                break
            deliver_interrupt(rt, (0x08 + n) if n < 8 else (0x70 + n - 8), max_steps=2_000_000)
            g += 1

    guard = 0
    while len(snaps) < frames and guard < det_speed * 60:
        if cpu.instruction_count % 2000 == 0:
            pump()
        cpu.step(); guard += 1
    return snaps


def main():
    snap = "artifacts/snapshot_pre2_20260625_170717"
    snaps = run(snap)
    print(f"captured {len(snaps)} source frames")
    # duplicate base_id check across frames
    dup_frames = 0
    for _ic, _cam, rows in snaps:
        ids = [r[0] for r in rows if not r[5]]
        if len(ids) != len(set(ids)):
            dup_frames += 1
    print(f"frames with duplicate non-HUD base_ids: {dup_frames}/{len(snaps)}  "
          f"(queue mis-pairing risk if >0)")
    print("\ncamera x_px per source frame:")
    cams = [c for (_ic, c, _r) in snaps]
    print("  " + " ".join(str(c) for c in cams[:18]))
    print("  cam deltas: " + " ".join(str(b - a) for a, b in list(zip(cams, cams[1:]))[:17]))
    # per base_id: world_x vs screen_x trajectories
    by_id = {}
    for fi, (_ic, cam, rows) in enumerate(snaps):
        for (bid, sid, flip, sx, sy, hud, mode, wx, wy) in rows:
            if hud:
                continue
            by_id.setdefault(bid, []).append((fi, cam, sx, wx))
    print("\nper non-HUD object: WORLD_x deltas vs SCREEN_x deltas (is the jitter in world motion or camera?):")
    for bid, tr in sorted(by_id.items()):
        wxs = [t[3] for t in tr]
        sxs = [t[2] for t in tr]
        wdel = [b - a for a, b in zip(wxs, wxs[1:])]
        sdel = [b - a for a, b in zip(sxs, sxs[1:])]
        if max((abs(d) for d in sdel), default=0) == 0:
            continue   # static object
        print(f"  id={bid:#06x} frames={len(tr)}")
        print(f"      world_x : {' '.join(str(x) for x in wxs[:14])}")
        print(f"      world_dx: {' '.join(str(d) for d in wdel[:13])}")
        print(f"      scrn_dx : {' '.join(str(d) for d in sdel[:13])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
