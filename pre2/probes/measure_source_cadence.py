"""Measure PRE2's actual gameplay SOURCE-frame cadence (the rate at which the game produces a meaningfully
new gameplay frame), independent of the 70 Hz VGA retrace — the foundation the enhanced renderer interpolates
on. Per the enhanced architecture, enhanced presents at the display refresh but only RE-RENDERS at the source
cadence; the in-between display subframes are interpolation, not faithful re-rasterization.

What it records, driving a gameplay snapshot with movement injected so camera + sprites actually move:
  * 6772  = the gameplay frame-commit boundary (palette-fade entry, POST page-flip) -> one per game frame.
  * For each commit: instruction_count, camera (cam_x tiles + fine_scroll px), and a signature of the live
    object-sprite world positions.
Then it reports the EMULATED-TIME interval between commits (-> source fps), how many 70 Hz retrace cycles fit
in one source frame (-> the display:source ratio the enhanced renderer interpolates across), and how often
the camera / sprite positions actually change.
"""
import sys
from collections import Counter

sys.path.insert(0, ".")

from dos_re.cpu import IF
from dos_re.interrupts import deliver_interrupt, deliver_scancode
from dos_re.runtime import enable_sound_blaster
from pre2.bridge.render_state import read_renderer_state
from pre2.recovered.render_snapshot import build_frame_snapshot
from pre2.runtime import load_pre2_snapshot

CS = 0x1030
_6772 = (0x1030, 0x6772)
RIGHT = 0x4D    # right-arrow scancode -> scroll the camera


def characterize(snap, present_hz=70, speed=150_000, frames=120, inject=False):
    """Per SOURCE frame (6772), capture the GameFrameSnapshot the enhanced renderer will consume and report
    what actually changes between source frames: camera, per-object screen positions (matched by base_id),
    animation frame, palette-fade phase/amount. This is the data the object-aware interpolation rides on."""
    rt = load_pre2_snapshot("assets/pre2.exe", snap, game_root="assets", native_replacements=True)
    cpu, dos = rt.cpu, rt.dos
    cpu.trace_enabled = False
    sb = enable_sound_blaster(rt, detection_only=True)
    pic = dos.pic
    det_speed = (speed // present_hz) * present_hz
    dos.time_source = lambda: cpu.instruction_count / det_speed
    dos.vga_retrace_active_fraction = 0.06
    tick = {"next": 0.0}
    snaps = []
    orig = cpu.replacement_hooks.get(_6772)
    from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook

    def on_commit(c):
        try:
            gs = build_frame_snapshot(read_renderer_state(c.mem, dos, game_root="assets"))
            objs = {s.base_id: (s.screen_x, s.screen_y) for s in gs.sprites if not s.is_hud}
            snaps.append((cpu.instruction_count, gs.camera.x_px, gs.camera.y_px, objs,
                          gs.animation.frame_index if gs.animation else None,
                          (int(gs.palette.phase), gs.palette.fade_amount) if gs.palette else None))
        except Exception as e:
            snaps.append((cpu.instruction_count, None, None, {}, None, None))
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

    guard = 0; last_inject = 0
    while len(snaps) < frames and guard < det_speed * 90:
        if cpu.instruction_count % 2000 == 0:
            pump()
            if inject and cpu.instruction_count - last_inject > 5000:
                deliver_scancode(rt, RIGHT, max_steps=2_000_000); last_inject = cpu.instruction_count
        cpu.step(); guard += 1

    if len(snaps) < 4:
        print(f"  only {len(snaps)} source frames"); return
    cam_moved = obj_moved_frames = anim_changed = fade_changed = 0
    max_obj_delta = 0; obj_move_counts = []
    for a, b in zip(snaps, snaps[1:]):
        if (a[1], a[2]) != (b[1], b[2]):
            cam_moved += 1
        moved = 0
        for bid, (bx, by) in b[3].items():
            if bid in a[3]:
                ax, ay = a[3][bid]
                d = max(abs(bx - ax), abs(by - ay))
                if d:
                    moved += 1; max_obj_delta = max(max_obj_delta, d)
        if moved:
            obj_moved_frames += 1; obj_move_counts.append(moved)
        if a[4] != b[4]:
            anim_changed += 1
        if a[5] != b[5]:
            fade_changed += 1
    n = len(snaps) - 1
    avg_objs = sum(len(s[3]) for s in snaps) / len(snaps)
    print(f"  source frames={len(snaps)}  avg live objects/frame={avg_objs:.1f}")
    print(f"  camera moved: {cam_moved}/{n} frames")
    print(f"  OBJECTS moved: {obj_moved_frames}/{n} frames; max screen delta/frame={max_obj_delta}px; "
          f"avg objects-moving when any={ (sum(obj_move_counts)/len(obj_move_counts)) if obj_move_counts else 0:.1f}")
    print(f"  animation frame changed: {anim_changed}/{n}; palette-fade phase/amount changed: {fade_changed}/{n}")


def run(snap, present_hz=70, speed=150_000, frames=120):
    rt = load_pre2_snapshot("assets/pre2.exe", snap, game_root="assets", native_replacements=True)
    cpu, dos = rt.cpu, rt.dos
    cpu.trace_enabled = False
    sb = enable_sound_blaster(rt, detection_only=True)
    pic = dos.pic
    det_speed = (speed // present_hz) * present_hz
    dos.time_source = lambda: cpu.instruction_count / det_speed
    dos.vga_retrace_active_fraction = 0.06
    tick = {"next": 0.0}
    sub_batch = 2000
    ic_per_retrace = det_speed / present_hz

    commits = []   # (ic, cam_x, fine, sprite_sig)
    orig = cpu.replacement_hooks.get(_6772)

    def on_commit(c):
        try:
            rs = read_renderer_state(c.mem, dos, game_root="assets")
            sig = tuple((s.x, s.y) for s in (rs.object_sprites or ())[:24])
            commits.append((cpu.instruction_count, rs.camera_x, rs.fine_scroll, sig))
        except Exception:
            commits.append((cpu.instruction_count, None, None, None))
        from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
        if orig is not None:
            return orig(c)
        interpret_current_instruction_without_hook(c)
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
    last_inject = 0
    while len(commits) < frames and guard < det_speed * 90:
        if cpu.instruction_count % sub_batch == 0:
            pump()
            if cpu.instruction_count - last_inject > 5000:   # hold the right-arrow down
                deliver_scancode(rt, RIGHT, max_steps=2_000_000)
                last_inject = cpu.instruction_count
        cpu.step(); guard += 1

    # analyse
    if len(commits) < 4:
        print(f"  only {len(commits)} commits captured (snapshot may not be running gameplay)"); return
    deltas = [b[0] - a[0] for a, b in zip(commits, commits[1:])]
    deltas.sort()
    med = deltas[len(deltas) // 2]
    cam_changes = sum(1 for a, b in zip(commits, commits[1:]) if (a[1], a[2]) != (b[1], b[2]))
    spr_changes = sum(1 for a, b in zip(commits, commits[1:]) if a[3] != b[3])
    n = len(deltas)
    print(f"  source commits (6772): {len(commits)}")
    print(f"  ic/source-frame: median={med}  min={deltas[0]}  max={deltas[-1]}")
    print(f"  source fps (emulated): {det_speed / med:.1f}   (retrace is {present_hz} Hz)")
    print(f"  -> retrace cycles per source frame: {med / ic_per_retrace:.2f}  "
          f"(= display:source interpolation ratio at {present_hz}Hz)")
    print(f"  camera changed on {cam_changes}/{n} frame-steps; sprite positions changed on {spr_changes}/{n}")
    hist = Counter(round(d / ic_per_retrace) for d in deltas)
    print(f"  retrace-cycles-per-frame histogram: {dict(sorted(hist.items()))}")


def main():
    print("### CADENCE (when does a new source frame commit) ###")
    for label, snap in (
        ("GAMEPLAY 185902 (+right-arrow)", "artifacts/snapshot_pre2_gameplay_20260621_185902"),
    ):
        print(f"=== {label} ==="); run(snap)
    print("\n### WHAT CHANGES per source frame (moving-sprite witnesses) ###")
    for label, snap, inj in (
        ("SHAKE/moving 144516", "artifacts/snapshot_pre2_20260623_144516", False),
        ("SPIDERS 112313", "artifacts/snapshot_pre2_spiders_20260624_112313", False),
        ("PLAYER-DEATH 103048", "artifacts/snapshot_pre2_player_death_20260624_103048", False),
        ("GAMEPLAY 185902 +scroll", "artifacts/snapshot_pre2_gameplay_20260621_185902", True),
    ):
        print(f"=== {label} ==="); characterize(snap, inject=inj)
    return 0


if __name__ == "__main__":
    sys.exit(main())
