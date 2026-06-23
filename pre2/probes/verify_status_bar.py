"""Verify pre2.recovered.hud.draw_status_bar vs the ASM static-bar blit at 1030:4580.

The HUD chrome bar bitmap (seg 0x252B:0x0B48) is loaded + blitted at level start, then that memory
region is REUSED (transient) — so we drive the map -> Space level load, capture the bar source at the
blit instant (actual ds:si at 4586), let the ASM blit finish (to the routine ret at 45AA, before the
dynamic values), and diff draw_status_bar(captured bar) against the ASM page bar region byte-exact.
"""
import sys; sys.path.insert(0, '.')
from pre2.runtime import load_pre2_snapshot
from dos_re.runtime import enable_sound_blaster
from dos_re.interrupts import deliver_interrupt, deliver_scancode
from dos_re.cpu import IF
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from pre2.recovered.hud import draw_status_bar, HUD_BAR_DI, HUD_BAR_PLANE_BYTES

_BLIT_PLANE = 0x4586   # plane-loop entry (di/si set for the current plane)
_BLIT_RET = 0x45AA     # the bar blit routine's ret (bar drawn to both pages, before draw_hud)
_BAR_LEN = 0xE60


def main():
    rt = load_pre2_snapshot('assets/pre2.exe', 'artifacts/snapshot_pre2_mapscroll_20260623_110253',
                            game_root='assets', native_replacements=True)
    cpu, m, dos = rt.cpu, rt.cpu.mem, rt.dos
    cpu.trace_enabled = False
    sb = enable_sound_blaster(rt, detection_only=True); pic = rt.dos.pic
    clock = lambda: cpu.instruction_count / (6428 * 70); dos.time_source = clock  # noqa: E731
    tick = {"next": clock()}

    def pump():
        now = clock(); tp = 1.0 / max(1.0, dos.pit_channel0_hz())
        while now >= tick["next"]:
            pic.raise_irq(0); tick["next"] += tp
            if tick["next"] < now - 0.25:
                tick["next"] = now + tp
        if sb:
            sb.service()
        g = 0
        while cpu.get_flag(IF) and g < 64:
            nn = pic.acknowledge()
            if nn is None:
                break
            deliver_interrupt(rt, (0x08 + nn) if nn < 8 else (0x70 + nn - 8), max_steps=2_000_000); g += 1

    def frame(sc=None):
        if sc is not None:
            try:
                deliver_scancode(rt, sc, max_steps=200000)
            except Exception:  # noqa: BLE001
                pass
        for _ in range(4):
            pump()
            for _ in range(1607):
                cpu.step()

    for f in range(160):
        frame(0x39 if 3 <= f <= 8 else None)   # Space: select/start the level from the map

    cap = None
    page = 0
    for i in range(200000):
        if i % 1500 == 0:
            pump()
        s = cpu.s
        if s.cs == 0x1030 and s.ip == _BLIT_PLANE and cap is None:
            src = ((s.ds << 4) + s.si) & 0xFFFFF
            cap = bytes(m.data[src:src + _BAR_LEN])
            page = (s.di - HUD_BAR_DI) & 0xFFFF
        if s.cs == 0x1030 and s.ip == _BLIT_RET and cap is not None:
            break
        cpu.step()

    if cap is None:
        print("bar blit not reached"); return 1
    rec = [bytearray(EGA_PLANE_STRIDE) for _ in range(4)]
    draw_status_bar(rec, page, cap)
    diff = sum(1 for p in range(4) for off in range(HUD_BAR_DI, HUD_BAR_DI + HUD_BAR_PLANE_BYTES)
               if rec[p][(page + off) & 0xFFFF] != m.data[EGA_APERTURE + p * EGA_PLANE_STRIDE + ((page + off) & 0xFFFF)])
    print(f"draw_status_bar vs ASM page bar region: diff={diff} / {4 * HUD_BAR_PLANE_BYTES} bytes")
    print("STATUS-BAR BLIT LOCKSTEP:", "PASS" if diff == 0 else "FAIL")
    return 0 if diff == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
