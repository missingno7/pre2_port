"""TEMPORARY probe — does enabling audio corrupt the rendered frame (offline)?

Cold-boots with the Sound Blaster enabled, drives to the title, and saves a
snapshot to render.  If the offline render is corrupt, the audio path corrupts
game state/memory; if it's clean, the live corruption is viewer presentation
timing (the VM falling behind -> partial frames).
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dos_re.interrupts import deliver_scancode
from dos_re.runtime import enable_sound_blaster
from dos_re.snapshot import write_snapshot
from pre2.runtime import create_pre2_runtime


def main() -> int:
    enable = "--no-sb" not in sys.argv
    rt = create_pre2_runtime(str(ROOT / "assets" / "pre2.exe"), game_root=str(ROOT / "assets"), fast_adlib=True)
    cpu = rt.cpu
    cpu.trace_enabled = False
    sb = pic = None
    if enable:
        sb = enable_sound_blaster(rt)
        pic = rt.dos.pic
        cpu.pending_irq = lambda: pic.acknowledge()  # clock stays None -> timer-paced playback

    for f in range(700):
        if pic is not None:
            pic.raise_irq(0)
            sb.service()
        for _ in range(4000):
            cpu.step()
        if f in (40, 60, 120, 180, 240) or (f > 40 and f % 150 == 0):
            deliver_scancode(rt, 0x1C, max_steps=100000)
        if f in (90, 150, 210, 300) or (f > 40 and f % 150 == 70):
            deliver_scancode(rt, 0x9C, max_steps=100000)
        if sb is not None and len([e for e in sb.log if e[0] == "dma_start"]) >= 40:
            break
        if sb is None and rt.dos.video_mode == 0x13 and f > 560:
            break

    tag = "audio" if enable else "nosb"
    out = ROOT / "artifacts" / f"render_{tag}"
    write_snapshot(rt, out, status=f"title with sb={enable}", steps=0)
    print(f"mode={rt.dos.video_mode:02X}h frame={f}; wrote {out}")
    if sb is not None:
        print(f"sb playback blocks={len([e for e in sb.log if e[0]=='dma_start'])} pcm={len(sb.pcm_out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
