"""Verify pre2.recovered.hud.draw_hud (the dynamic status-bar layout, 1030:45B8) vs the ASM.

draw_hud lays out the lives digit / 6-digit score (+trailing 0) / energy hearts from HudState via
the verified glyph blit. The ASM already drew those glyphs into the page; this drives a gameplay
snapshot, runs draw_hud onto a CLEAN framebuffer fed only HudState + the font, and diffs the glyph
regions (lives/score/energy) against the page byte-exact. The static status-bar background between
the glyphs is excluded (drawn separately, not by draw_hud).
"""
import sys; sys.path.insert(0, '.'); sys.path.insert(0, 'scripts')
from pre2.runtime import load_pre2_snapshot
from dos_re.runtime import enable_sound_blaster
from dos_re.interrupts import deliver_interrupt
from dos_re.cpu import IF
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE
from pre2.bridge.render_state import read_renderer_state
from pre2.recovered.hud import (
    draw_hud, HUD_LIVES_DI, HUD_SCORE_DI, HUD_ENERGY_DI, HUD_MAX_HEARTS, HUD_GLYPH_ROWS,
)

_DS = 0x1A0F


def main():
    rt = load_pre2_snapshot('assets/pre2.exe', 'artifacts/snapshot_pre2_gameplay_20260621_185902',
                            game_root='assets', native_replacements=True)
    cpu, m, dos = rt.cpu, rt.cpu.mem, rt.dos
    cpu.trace_enabled = False
    sb = enable_sound_blaster(rt, detection_only=True); pic = rt.dos.pic
    clock = lambda: cpu.instruction_count / (6428 * 70); dos.time_source = clock  # noqa: E731
    tick = {"next": clock()}

    def frame():
        r = 6428
        while r > 0:
            n = min(2000, r); now = clock(); tp = 1.0 / max(1.0, dos.pit_channel0_hz())
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
            for _ in range(n):
                cpu.step()
            r -= n

    for _ in range(3):
        frame()
    hud = read_renderer_state(m, dos).hud_state
    fontseg = m.data[(_DS << 4) + 0x3d] | (m.data[(_DS << 4) + 0x3e] << 8)
    font = bytes(m.data[(fontseg << 4):(fontseg << 4) + 0x4000])
    print(f"HudState: score={hud.score} lives={hud.lives} energy={hud.energy}; font seg={fontseg:#06x}")

    glyph_dis = ([HUD_LIVES_DI]
                 + [HUD_SCORE_DI + 2 * i for i in range(7)]
                 + [HUD_ENERGY_DI + 2 * i for i in range(HUD_MAX_HEARTS)])

    for page_name, page in (("display_start", rt.program.memory.ega_display_start),
                            ("dest_page[0x2DD8]", m.data[(_DS << 4) + 0x2DD8] | (m.data[(_DS << 4) + 0x2DD9] << 8))):
        rec = [bytearray(EGA_PLANE_STRIDE) for _ in range(4)]
        draw_hud(rec, hud, font, page=page)
        diff = 0
        for di in glyph_dis:
            for p in range(4):
                apbase = EGA_APERTURE + p * EGA_PLANE_STRIDE
                for row in range(HUD_GLYPH_ROWS):
                    for b in range(2):
                        off = (page + di + row * 0x28 + b) & 0xFFFF
                        if rec[p][off] != m.data[apbase + off]:
                            diff += 1
        print(f"  page={page_name} ({page:#06x}): glyph-region diff = {diff} bytes")
        if diff == 0:
            print("HUD LAYOUT (draw_hud) vs ASM:", "PASS (byte-exact glyphs)")
            return 0
    print("HUD LAYOUT (draw_hud) vs ASM: FAIL")
    return 1


if __name__ == "__main__":
    sys.exit(main())
