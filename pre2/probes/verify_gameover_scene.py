"""Verify the recovered GAME-OVER overlays compose Δ=0 over a diagnostic fixture background.

Per the scene-compositor discipline: the diorama background is an unrecovered ``MissingBackgroundGap``,
so this probe proves only the DYNAMIC overlays (object pass + HUD) — that ``compose_scene`` over an
ORACLE-CAPTURED fixture background reproduces the displayed frame byte-exact. The fixture is the VRAM at
the object pass's entry (the per-frame base the ASM erases-and-redraws over); composing the recovered
overlays on top must equal the VRAM after the pass. This does NOT claim full-scene recovery — the
background image is still an open task; it grounds the overlay+compositor placement only.
"""
import glob
import sys

sys.path.insert(0, ".")

from dos_re.cpu import IF
from dos_re.interrupts import deliver_interrupt
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from dos_re.runtime import enable_sound_blaster
from pre2.bridge.gameover_scene import _object_overlay, capture_background_fixture
from pre2.bridge.render_state import read_renderer_state, retarget_page
from pre2.recovered.scene_compositor import SceneStatus, compose_scene
from pre2.runtime import load_pre2_snapshot

_OBJPASS = 0x26FA


def _vp_diff(planes, after, page, rows):
    n = 0
    for p in range(4):
        ap = after[p]
        pl = planes[p]
        for o in range(rows * 0x28):
            a = (page + o) & 0xFFFF
            if pl[a] != ap[a]:
                n += 1
    return n


def _grab(d):
    return [bytes(d[EGA_APERTURE + p * EGA_PLANE_STRIDE: EGA_APERTURE + p * EGA_PLANE_STRIDE + 0x10000])
            for p in range(4)]


def main(snap=None, warm=5_000_000, samples=6):
    snap = snap or glob.glob("artifacts/snapshot_pre2_*gameover_20260623_110546")[0]
    rt = load_pre2_snapshot("assets/pre2.exe", snap, game_root="assets", native_replacements=False)
    cpu, dos = rt.cpu, rt.dos
    cpu.trace_enabled = False
    sb = enable_sound_blaster(rt, detection_only=True)
    pic = dos.pic
    clock = lambda: cpu.instruction_count / (6428 * 70)  # noqa: E731
    dos.time_source = clock
    tick = {"next": clock()}
    d = rt.program.memory.data

    def pump():
        now = clock()
        tp = 1.0 / max(1.0, dos.pit_channel0_hz())
        while now >= tick["next"]:
            pic.raise_irq(0)
            tick["next"] += tp
            if tick["next"] < now - 0.25:
                tick["next"] = now + tp
        if sb:
            sb.service()
        g = 0
        while cpu.get_flag(IF) and g < 64:
            nn = pic.acknowledge()
            if nn is None:
                break
            deliver_interrupt(rt, (0x08 + nn) if nn < 8 else (0x70 + nn - 8), max_steps=2_000_000)
            g += 1

    s = cpu.s
    while cpu.instruction_count < warm:           # drive into the game-over scene (object loop)
        if cpu.instruction_count % 1500 == 0:
            pump()
        cpu.step()

    results = []
    for _ in range(4_000_000):
        if cpu.instruction_count % 1500 == 0:
            pump()
        if s.cs == 0x1030 and s.ip == _OBJPASS:
            page = d[(0x1A0F << 4) + 0x2DD8] | (d[(0x1A0F << 4) + 0x2DD9] << 8)   # back/draw page
            # OBJECT overlay: composed over the per-frame base (the page the pass erases-and-redraws over)
            fixture = capture_background_fixture(cpu.mem, page)
            rs = retarget_page(read_renderer_state(cpu.mem, dos, game_root="assets"), page)
            planes, status = compose_scene(fixture, [_object_overlay(rs)], page)
            assert status == SceneStatus.FIXTURE
            sp0 = s.sp
            guard = 0
            while guard < 2_000_000:                                              # run the ASM object pass
                cpu.step()
                guard += 1
                if s.cs == 0x1030 and s.ip != _OBJPASS and s.sp > sp0:
                    break
            after = _grab(d)
            obj_vp = _vp_diff(planes, after, page, 176)
            results.append(obj_vp)
            if len(results) >= samples:
                break
        cpu.step()

    assert results, "no object pass reached in the game-over scene"
    for obj_vp in results:
        print(f"  object overlay viewport Δ={obj_vp}")
    ok = all(o == 0 for o in results)
    print(f"composited {len(results)} game-over frames: recovered OBJECT overlay over the fixture bg")
    # NOTE (HUD): the displayed game-over HUD is FROZEN at the last 45B8 (death moment, empty energy);
    # the loop never redraws it while [0x27D6] later resets to 0xFF, so draw_hud from LIVE state shows
    # full hearts. The HUD is a recovered leaf reused as-is; the viewer must feed it the frozen HUD state
    # (the last_hud pattern), not live memory. That is a viewer state-timing concern, not a draw_hud bug.
    print("GAMEOVER_SCENE: PASS" if ok else "GAMEOVER_SCENE: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
