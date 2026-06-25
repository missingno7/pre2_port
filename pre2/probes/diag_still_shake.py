"""Diagnose 'still object shakes': dump each active-list SLOT's (screen_x, screen_y) per source frame so we
can see whether a supposedly-still object's source placement oscillates (which interpolation then amplifies
into visible shaking). Reads slot = enumerate index of the active list (the stable identity) and the placement
via plan_sprite_command (screen_x, top_row)."""
import sys

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

from dos_re.cpu import IF
from dos_re.interrupts import deliver_interrupt
from dos_re.runtime import enable_sound_blaster
from pre2.bridge.render_state import read_renderer_state
from pre2.recovered.object_render import plan_sprite_command
from pre2.runtime import load_pre2_snapshot

_6772 = (0x1030, 0x6772)


def run(snap, frames=16):
    rt = load_pre2_snapshot("assets/pre2.exe", snap, game_root="assets", native_replacements=True)
    cpu, dos = rt.cpu, rt.dos
    cpu.trace_enabled = False
    sb = enable_sound_blaster(rt, detection_only=True)
    pic = dos.pic
    ds = 6428 * 70
    dos.time_source = lambda: cpu.instruction_count / ds
    dos.vga_retrace_active_fraction = 0.06
    tick = {"next": 0.0}
    out = []
    orig = cpu.replacement_hooks.get(_6772)
    from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook

    def hook(c):
        try:
            rs = read_renderer_state(c.mem, dos, game_root="assets")
            cam = rs.object_camera
            d = {}
            for slot, spr in enumerate(rs.object_sprites or ()):
                attr = (rs.object_attrs or {}).get(spr.sprite_id)
                if attr is None:
                    continue
                cmd = plan_sprite_command(spr, attr, cam)
                if cmd is None or cmd.is_hud:
                    continue
                d[slot] = (cmd.screen_x, cmd.screen_y, cmd.sprite_id, spr.x, spr.y)
            out.append((cam.row_factor, cam.fine_scroll, cam.cam_x, cam.cam_y, d))
        except Exception as e:
            out.append((None, None, None, None, {}))
        return orig(c) if orig else interpret_current_instruction_without_hook(c)
    cpu.replacement_hooks[_6772] = hook

    def pump():
        now = cpu.instruction_count / ds
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

    g = 0
    while len(out) < frames and g < ds * 40:
        if cpu.instruction_count % 2000 == 0:
            pump()
        cpu.step(); g += 1
    return out


def report(label, snap):
    fr = run(snap)
    print(f"=== {label} ({len(fr)} source frames) ===")
    print("  cam row_factor seq: " + " ".join(str(f[0]) for f in fr[:14]))
    print("  cam fine_scroll seq: " + " ".join(str(f[1]) for f in fr[:14]))
    slots = {}
    for fi, (_rf, _fs, _cx, _cy, d) in enumerate(fr):
        for slot, (sx, sy, sid, wx, wy) in d.items():
            slots.setdefault(slot, []).append((sx, sy, sid, wx, wy))
    for slot, tr in sorted(slots.items()):
        sxs = [t[0] for t in tr]; sys_ = [t[1] for t in tr]
        wxs = [t[3] for t in tr]; wys = [t[4] for t in tr]
        sdx = [b - a for a, b in zip(sxs, sxs[1:])]; sdy = [b - a for a, b in zip(sys_, sys_[1:])]
        wdx = [b - a for a, b in zip(wxs, wxs[1:])]; wdy = [b - a for a, b in zip(wys, wys[1:])]
        # flag oscillation: a delta sequence that changes sign (back-and-forth) on a ~still object
        def osc(dels):
            return any(a * b < 0 for a, b in zip(dels, dels[1:]))
        tag = ""
        if osc(sdx) or osc(sdy):
            tag = "  <-- SCREEN oscillates"
        if osc(wdx) or osc(wdy):
            tag += "  (world oscillates too)"
        print(f"  slot {slot}: screen_dx={sdx[:9]} screen_dy={sdy[:9]} world_dx={wdx[:9]} world_dy={wdy[:9]}{tag}")


def main():
    report("171332 (movement)", "artifacts/snapshot_pre2_20260625_171332")
    report("170717 (shakier)", "artifacts/snapshot_pre2_20260625_170717")
    return 0


if __name__ == "__main__":
    sys.exit(main())
