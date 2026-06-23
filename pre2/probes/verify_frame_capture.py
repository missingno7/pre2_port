"""End-to-end check of the capture seam (pre2/bridge/frame_capture.FrameCapture).

Drives a gameplay snapshot, ticks FrameCapture once per frame, and verifies it keeps the last
two GameFrameSnapshots and that interpolate(t=0.5) produces a frame whose camera + matched
sprite positions lie between the two captured frames (the inter-frame interpolation a future
enhanced renderer consumes).
"""
import sys; sys.path.insert(0, '.')
from pre2.runtime import load_pre2_snapshot
from pre2.bridge.frame_capture import FrameCapture
from dos_re.runtime import enable_sound_blaster
from dos_re.interrupts import deliver_interrupt
from dos_re.cpu import IF


def _driver(rt):
    cpu, dos = rt.cpu, rt.dos
    sb = enable_sound_blaster(rt, detection_only=True)
    pic = rt.dos.pic
    chunk = 6428
    clock = lambda: cpu.instruction_count / (chunk * 70)   # noqa: E731
    dos.time_source = clock
    tick = {"next": clock()}

    def frame():
        r = chunk
        while r > 0:
            n = min(2000, r)
            now = clock()
            tp = 1.0 / max(1.0, dos.pit_channel0_hz())
            while now >= tick["next"]:
                pic.raise_irq(0)
                tick["next"] += tp
                if tick["next"] < now - 0.25:
                    tick["next"] = now + tp
            if sb is not None:
                sb.service()
            g = 0
            while cpu.get_flag(IF) and g < 64:
                nn = pic.acknowledge()
                if nn is None:
                    break
                deliver_interrupt(rt, (0x08 + nn) if nn < 8 else (0x70 + nn - 8), max_steps=2_000_000)
                g += 1
            for _ in range(n):
                cpu.step()
            r -= n
    return frame


def _between(m, a, b):
    return min(a, b) <= m <= max(a, b)


def main():
    rt = load_pre2_snapshot('assets/pre2.exe', 'artifacts/snapshot_pre2_20260623_144516',
                            game_root='assets', native_replacements=True)
    rt.cpu.trace_enabled = False
    frame = _driver(rt)
    cap = FrameCapture()
    for _ in range(10):
        frame()
        cap.tick(rt.cpu.mem, rt.dos)

    assert cap.prev is not None and cap.cur is not None, "did not capture two frames"
    mid = cap.interpolated(0.5)
    cam_ok = (_between(mid.camera.x_px, cap.prev.camera.x_px, cap.cur.camera.x_px)
              and _between(mid.camera.y_px, cap.prev.camera.y_px, cap.cur.camera.y_px))
    # check matched sprites' midpoints lie between the two frames
    prev_by = {}
    for s in cap.prev.sprites:
        prev_by.setdefault(s.base_id, s)
    cur_by = {s.base_id for s in cap.cur.sprites}
    matched = [s for s in mid.sprites if s.base_id in prev_by and s.base_id in cur_by]
    spr_ok = all(_between(s.screen_x, prev_by[s.base_id].screen_x,
                          next(c for c in cap.cur.sprites if c.base_id == s.base_id).screen_x)
                 for s in matched)
    print(f"captured 2 frames: prev_sprites={len(cap.prev.sprites)} cur_sprites={len(cap.cur.sprites)}")
    print(f"camera prev={cap.prev.camera.x_px,cap.prev.camera.y_px} cur={cap.cur.camera.x_px,cap.cur.camera.y_px} mid={mid.camera.x_px,mid.camera.y_px}")
    print(f"matched sprites interpolated: {len(matched)}")
    print(f"camera midpoint between = {cam_ok};  sprite midpoints between = {spr_ok}")
    ok = cam_ok and spr_ok and len(cap.cur.sprites) > 0
    print("FRAME CAPTURE SEAM:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
