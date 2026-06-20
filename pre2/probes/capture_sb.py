"""TEMPORARY probe — validate the integrated Sound Blaster (dos.py + PIC + inline IRQ).

Cold-boots PRE2 with the emulated SB enabled via the real runtime path
(enable_sound_blaster), drives the boot, and checks that the driver detects the
card and streams PCM continuously (the PIC now holds/delivers IRQs and the CPU
delivers them inline). Reports the DSP command log, the captured PCM, and the
number of playback blocks.

Run:  python -m pre2.probes.capture_sb
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dos_re.interrupts import deliver_scancode
from dos_re.runtime import enable_sound_blaster
from pre2.runtime import create_pre2_runtime


def main() -> int:
    rt = create_pre2_runtime(str(ROOT / "assets" / "pre2.exe"), game_root=str(ROOT / "assets"), fast_adlib=True)
    cpu = rt.cpu
    cpu.trace_enabled = False
    sb = enable_sound_blaster(rt)
    pic = rt.dos.pic
    cpu.pending_irq = lambda: pic.acknowledge()  # inline delivery for the headless detection loop

    CH = 4000
    detected_at = None
    held = [False]
    for f in range(1400):
        try:
            pic.raise_irq(0)          # PIT IRQ0 once per frame (delivered inline)
            for _ in range(CH):
                cpu.step()
            # Enter press-and-hold every ~120 frames to advance each attract screen.
            if f > 40:
                want = (f % 120) < 50
                if want and not held[0]:
                    deliver_scancode(rt, 0x1C, max_steps=100000); held[0] = True
                elif not want and held[0]:
                    deliver_scancode(rt, 0x9C, max_steps=100000); held[0] = False
        except Exception as exc:  # noqa: BLE001
            print(f"stopped frame {f}: {type(exc).__name__}: {exc}")
            break
        if detected_at is None and any(e[0] == "reset" for e in sb.log):
            detected_at = f
        if f >= 1300:
            break

    from dos_re.snapshot import write_snapshot
    write_snapshot(rt, ROOT / "artifacts" / "render_audio", status="title with audio playing", steps=0)
    nblocks = len([e for e in sb.log if e[0] == "dma_start"])
    rates = sorted({e[1]["rate"] for e in sb.log if e[0] == "dma_start"})
    print(f"detected_at frame {detected_at}; mode={rt.dos.video_mode:02X}h")
    print(f"playback blocks={nblocks} rates={rates} pcm_captured={len(sb.pcm_out)} bytes "
          f"({len(sb.pcm_out)/8403:.2f}s @8.4kHz)")
    print("--- DSP command log (last 20) ---")
    for e in sb.log[-20:]:
        print("  ", e)
    if sb.pcm_out:
        import statistics, wave
        print(f"pcm sample range: min={min(sb.pcm_out)} max={max(sb.pcm_out)} "
              f"mean={statistics.mean(sb.pcm_out):.1f} (silence would be ~128 flat)")
        from collections import Counter
        rate = Counter(e[1]["rate"] for e in sb.log if e[0] == "dma_start").most_common(1)[0][0] or 8403
        out = ROOT / "artifacts" / "sb_capture.wav"
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(1)            # 8-bit unsigned (the SB DMA format)
            w.setframerate(rate)
            w.writeframes(bytes(sb.pcm_out))
        print(f"wrote {out} ({len(sb.pcm_out)} samples @ {rate} Hz)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
