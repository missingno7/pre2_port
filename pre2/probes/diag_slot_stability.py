"""Diagnose object-id switching during scroll: is the active-list SLOT a stable per-object identity, or do
objects relocate between record slots (so my enumerate-index matching pairs the wrong objects)?

Dumps the full active list per source frame (slot, full record bytes) so we can see whether an object keeps
its record slot across frames, and whether any record byte is a stable per-object id/handle/link."""
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
DSEG = 0x1A0F


def run(snap, frames=12):
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
        recs = {}
        try:
            rs = read_renderer_state(c.mem, dos, game_root="assets")
            cam = rs.object_camera
            for slot, spr in enumerate(rs.object_sprites or ()):
                attr = (rs.object_attrs or {}).get(spr.sprite_id)
                if attr is None:
                    continue
                cmd = plan_sprite_command(spr, attr, cam)   # valid + on-screen objects only
                if cmd is None or cmd.is_hud:
                    continue
                recs[slot] = (cmd.base_id, cmd.world_x, cmd.world_y)
        except Exception:
            pass
        cx = c.mem.data[(DSEG << 4) + 0x2DE4]
        out.append((cx, recs))
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


def main():
    fr = run("artifacts/snapshot_pre2_20260625_173150")
    print(f"{len(fr)} source frames; cam_x(tiles): " + " ".join(str(f[0]) for f in fr))
    print("\nVALID on-screen objects per frame (slot: base_id@world_x):")
    for fi, (cx, recs) in enumerate(fr):
        items = " ".join(f"{s}:{r[0]:#05x}@{r[1]}" for s, r in sorted(recs.items()))
        print(f"  f{fi} cam={cx} n={len(recs)}: {items}")
    # Slot-stability test: match objects across consecutive frames by NEAREST world pos (same base graphic),
    # and report when the matched object's SLOT changed (= my slot identity is wrong for that transition).
    print("\nslot-stability across frames (object matched by nearest world pos + same base_id):")
    for a, b in zip(fr, fr[1:]):
        _ca, ra = a; _cb, rb = b
        switches = 0
        for sb_slot, (bid, bx, by) in rb.items():
            # nearest prev object with same base_id
            cands = [(abs(ax - bx) + abs(ay - by), aslot) for aslot, (abid, ax, ay) in ra.items() if abid == bid]
            if not cands:
                continue
            d, aslot = min(cands)
            if d <= 12 and aslot != sb_slot:    # same object (close), but different slot
                switches += 1
        print(f"  f{_ca}->{_cb}: objects={len(rb)} slot-switches={switches}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
