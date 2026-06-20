"""TEMPORARY probe — in-VM lockstep verify of the sprite-decode replacements.

Replays the menu->level demo with the hooks flipped into verify mode: the original
ASM runs as the oracle and each native sprite-decode result (42F7 local + 436A
shared) is diffed against the ASM at the routine's RET. Asserts zero divergence.

Run:  python -m pre2.probes.verify_sprite_decode
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dos_re.input_demo import InputDemoPlayback
from dos_re.interrupts import deliver_scancode
from pre2.checkpoints import enable_pre2_hook_verification
from pre2.runtime import load_pre2_snapshot

DEMO = ROOT / "artifacts" / "demo_pre2_20260620_091827"


def main() -> int:
    playback = InputDemoPlayback.load(DEMO)
    meta = playback.manifest.get("metadata", {})
    chunk = int(meta.get("chunk_steps", 4000))
    rt = load_pre2_snapshot(
        ROOT / "assets" / "pre2.exe",
        playback.snapshot_path(),
        game_root=ROOT / "assets",
        fast_adlib=bool(meta.get("fast_adlib", False)),
    )
    rt.cpu.trace_enabled = False

    results = []
    stats = enable_pre2_hook_verification(
        rt,
        on_result=lambda name, ok, why: results.append((name, ok, why)),
        raise_on_divergence=False,
    )

    frame = 0
    while frame < 3000:
        playback.apply_to_runtime(frame, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=2000))
        try:
            for _ in range(chunk):
                rt.cpu.step()
        except Exception as exc:  # noqa: BLE001
            print(f"stopped at frame {frame}: {type(exc).__name__}: {exc}")
            break
        got = {n for n, _, _ in results}
        if "sprite_decode_local" in got and "sprite_decode_shared" in got:
            break
        frame += 1

    print(f"verified={stats.verified} diverged={stats.diverged}")
    for name, ok, why in results:
        print(f"  {name}: {'OK' if ok else 'DIVERGED: ' + str(why)}")
    sprite = [r for r in results if r[0].startswith("sprite_decode_")]
    ok = (len(sprite) >= 2 and all(r[1] for r in sprite) and not stats.diverged)
    print("SPRITE DECODE LOCKSTEP:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
