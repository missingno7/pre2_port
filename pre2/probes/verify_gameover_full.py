"""Verify the FULL recovered game-over scene Δ=0 — no VM framebuffer dependency.

The recovered background is composed from the decoded GAMEOVER.SQZ asset (NOT captured from VRAM); the
object overlay is the already-grounded object pass. This probe drives a game-over frame: it captures the
scroll [0x6BC4] that 9C87 used (the counter increments after, at 9CCD), runs the frame to the page flip
(44FB) so the back page holds the complete frame, then composes ``build_gameover_scene`` (recovered
diorama background + object overlay) and asserts the viewport matches the ASM back page byte-exact.

The HUD is intentionally excluded from the Δ=0 assertion: the displayed game-over HUD is frozen at the
last 45B8 (death moment) while live state drifts, so it needs the frozen-HUD feed (last_hud), a viewer
concern verified separately.
"""
import glob
import sys

sys.path.insert(0, ".")

from dos_re.cpu import IF
from dos_re.interrupts import deliver_interrupt
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from dos_re.runtime import enable_sound_blaster
from pre2.bridge.gameover_scene import build_gameover_scene, load_gameover_asset
from pre2.recovered.gameover_background import render_gameover_background
from pre2.recovered.scene_compositor import RecoveredBackground, SceneStatus
from pre2.runtime import load_pre2_snapshot

_PRESENT = 0x9C87        # background present (reads [0x6BC4])
_FLIP = 0x44FB           # page flip (frame complete on the back page)
_DATA = 0x1A0F


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
    asset = load_gameover_asset("assets")

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
    while cpu.instruction_count < warm:
        if cpu.instruction_count % 1500 == 0:
            pump()
        cpu.step()

    results = []
    pend = None
    for _ in range(4_000_000):
        if cpu.instruction_count % 1500 == 0:
            pump()
        if s.cs == 0x1030 and s.ip == _PRESENT and pend is None:
            scroll = d[(_DATA << 4) + 0x6BC4]
            page = d[(_DATA << 4) + 0x2DD8] | (d[(_DATA << 4) + 0x2DD9] << 8)
            pend = (scroll, page)
        elif pend is not None and s.cs == 0x1030 and s.ip == _FLIP:
            scroll, page = pend
            after = _grab(d)
            bg = RecoveredBackground(tuple(bytes(pl) for pl in render_gameover_background(asset, scroll, page)))
            planes, status = build_gameover_scene(cpu.mem, dos, game_root="assets", page=page, background=bg)
            assert status == SceneStatus.COMPLETE
            vp = sum(1 for p in range(4) for o in range(176 * 0x28)
                     if planes[p][(page + o) & 0xFFFF] != after[p][(page + o) & 0xFFFF])
            results.append((scroll, vp))
            pend = None
            if len(results) >= samples:
                break
        cpu.step()

    assert results, "no game-over frame reached"
    for scroll, vp in results:
        print(f"  scroll={scroll}  full-scene viewport Δ={vp}")
    ok = all(vp == 0 for _, vp in results)
    print(f"recovered background (GAMEOVER.SQZ) + object overlay over {len(results)} frames, NO VM framebuffer")
    print("GAMEOVER_FULL: PASS" if ok else "GAMEOVER_FULL: FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
