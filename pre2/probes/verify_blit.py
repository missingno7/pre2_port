"""TEMPORARY probe — in-VM lockstep verify of the sprite-blit replacement.

Replays into gameplay with hooks in verify mode: the original ASM draws (oracle)
and each native blit (1030:3B69, all three dispatch paths) is diffed against the
ASM framebuffer + exit di at the routine's RET. Caps the number of verified blits
(the blit fires ~700x/frame) and asserts zero divergence.

Run:  python -m pre2.probes.verify_blit
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from dos_re.input_demo import InputDemoPlayback
from dos_re.interrupts import deliver_scancode
from pre2.checkpoints import enable_pre2_hook_verification, _BLIT_ENTRY, _BLIT_EXITS
from pre2.runtime import load_pre2_snapshot

DEMO = ROOT / "artifacts" / "demo_pre2_20260620_091827"
LIMIT = 4000      # safety cap on total verified blits
PER_TYPE = 8      # stop once every dispatch path has this many verified blits


def main() -> int:
    playback = InputDemoPlayback.load(DEMO)
    meta = playback.manifest.get("metadata", {})
    chunk = int(meta.get("chunk_steps", 4000))
    rt = load_pre2_snapshot(
        ROOT / "assets" / "pre2.exe", playback.snapshot_path(),
        game_root=ROOT / "assets", fast_adlib=bool(meta.get("fast_adlib", False)),
    )
    cpu = rt.cpu
    cpu.trace_enabled = False

    by_type: dict[str, int] = {}
    state = {"blits": 0}

    def _stop_verifying():
        for k in (_BLIT_ENTRY, *_BLIT_EXITS):
            cpu.replacement_hooks.pop(k, None)
            cpu.hook_names.pop(k, None)

    def on_result(name, ok, why):
        if name.startswith("sprite_blit_type"):
            state["blits"] += 1
            by_type[name] = by_type.get(name, 0) + (1 if ok else 0)
            # cover all three paths (type0 plain / type1 empty / type>=2 masked).
            paths = {n[len("sprite_blit_"):] for n in by_type}
            enough = sum(1 for n in ("type0", "type1") if by_type.get(n, 0) >= PER_TYPE)
            masked = any(n not in ("type0", "type1") and by_type[n] >= PER_TYPE for n in by_type)
            if (enough == 2 and masked) or state["blits"] >= LIMIT:
                _stop_verifying()

    stats = enable_pre2_hook_verification(rt, on_result=on_result, raise_on_divergence=False)

    frame = 0
    while frame < 3000:
        playback.apply_to_runtime(frame, rt, deliver=lambda r, sc: deliver_scancode(r, sc, max_steps=2000))
        try:
            for _ in range(chunk):
                cpu.step()
        except Exception as exc:  # noqa: BLE001
            print(f"stopped at frame {frame}: {type(exc).__name__}: {exc}")
            break
        if _BLIT_ENTRY not in cpu.replacement_hooks:  # stopped verifying
            break
        frame += 1

    blit_div = [d for d in stats.diverged if d[0].startswith("sprite_blit")]
    print(f"blits verified={state['blits']} by_type={by_type}")
    print(f"blit divergences={blit_div}")
    ok = not blit_div and len(by_type) >= 3
    print("SPRITE BLIT LOCKSTEP:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
