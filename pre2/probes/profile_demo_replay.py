"""Replay a recorded demo deterministically through the enhanced pipeline and time each SOURCE frame (the
6772 commit: extract + compose) to locate the occasional slowdowns. Correlates spikes with cache-miss deltas
(tile decode / sprite paint / HUD re-render / native fallback). Run:
    python pre2/probes/profile_demo_replay.py artifacts/demo_pre2_20260626_001513
"""
import argparse
import sys
from pathlib import Path
from time import perf_counter

sys.path.insert(0, ".")
sys.path.insert(0, "scripts")

import numpy as np

from dos_re.input_demo import InputDemoPlayback
from dos_re.interrupts import deliver_scancode
from play import _advance_frame_deterministic, _make_replay_runtime
from pre2.bridge.faithful_session import FaithfulSession


def main():
    demo = sys.argv[1] if len(sys.argv) > 1 else "artifacts/demo_pre2_20260626_001513"
    playback = InputDemoPlayback.load(Path(demo))
    args = argparse.Namespace(exe="assets/pre2.exe", game_root="assets", audio="off", fast_adlib=False,
                              timer_irq=True, input_irq_steps=2_000_000, steps=None, chunk_steps=1250,
                              present_hz=120, retrace_pulse=0.06, verify=False)
    rt = _make_replay_runtime(args, playback)

    session = FaithfulSession(rt, args, verify=False)
    session.install_hooks()
    session.enhanced_capture = True
    session.enh_clock = perf_counter
    session.async_extract = False                # synchronous inline extraction (deterministic timing)
    tile_cache = session._bg_cache[0]
    spr_cache = session._sprite_tex_cache

    # wrap the 6772 boundary to time the whole source-frame extraction + record cache-stat deltas
    orig = rt.cpu.replacement_hooks[(0x1030, 0x6772)]
    samples = []   # (frame, ms, d_tile_miss, d_spr_miss, d_hud_miss, d_fallback)

    def timed(c):
        s0 = (tile_cache.stats["misses"], spr_cache.stats["misses"],
              tile_cache.stats["hud_misses"], tile_cache.stats["fallbacks"])
        t0 = perf_counter()
        r = orig(c)
        dt = (perf_counter() - t0) * 1000.0
        s1 = (tile_cache.stats["misses"], spr_cache.stats["misses"],
              tile_cache.stats["hud_misses"], tile_cache.stats["fallbacks"])
        samples.append((len(samples), dt, s1[0] - s0[0], s1[1] - s0[1], s1[2] - s0[2], s1[3] - s0[3]))
        return r

    rt.cpu.replacement_hooks[(0x1030, 0x6772)] = timed

    det_speed = max(1, int(args.chunk_steps) * max(1, int(args.present_hz)))
    det_now = lambda: rt.cpu.instruction_count / det_speed   # noqa: E731
    rt.dos.time_source = det_now
    tick_state = {"next": 0.0}
    frame = 0
    while not playback.finished(frame):
        playback.apply_to_runtime(frame, rt,
                                  deliver=lambda runtime, sc: deliver_scancode(runtime, sc,
                                                                               max_steps=args.input_irq_steps))
        try:
            _advance_frame_deterministic(rt, args, chunk_steps=args.chunk_steps, sub_batch=2000,
                                         clock=det_now, pic=rt.dos.pic, sound_blaster=None,
                                         timer_irq=args.timer_irq, input_irq_steps=args.input_irq_steps,
                                         tick_state=tick_state, det_speed=det_speed)
        except Exception as e:
            print(f"stopped at frame {frame}: {type(e).__name__}: {e}")
            break
        frame += 1

    ms = np.array([s[1] for s in samples])
    if len(ms) == 0:
        print("no source frames captured"); return 1
    print(f"demo: {demo}   source frames timed: {len(ms)}   (game frames replayed: {frame})")
    print(f"  extract+compose per source frame (ms): mean={ms.mean():.2f} p50={np.percentile(ms,50):.2f} "
          f"p90={np.percentile(ms,90):.2f} p99={np.percentile(ms,99):.2f} max={ms.max():.2f}")
    thr = max(np.percentile(ms, 95), ms.mean() * 2)
    spikes = [s for s in samples if s[1] >= thr]
    print(f"  spikes (>= {thr:.2f}ms): {len(spikes)} frames")
    print(f"  {'frame':>6} {'ms':>7} {'tileMiss':>9} {'sprMiss':>8} {'hudMiss':>8} {'fallback':>9}")
    for s in sorted(spikes, key=lambda x: -x[1])[:15]:
        print(f"  {s[0]:>6} {s[1]:>7.2f} {s[2]:>9} {s[3]:>8} {s[4]:>8} {s[5]:>9}")
    # correlation summary
    sp = np.array([s for s in samples])
    hud_frames = int((sp[:, 4] > 0).sum())
    fb_frames = int((sp[:, 5] > 0).sum())
    print(f"  totals: HUD re-renders={hud_frames} frames, native fallbacks={fb_frames} frames, "
          f"tile_hit={tile_cache.hit_rate()*100:.0f}% spr_hit={spr_cache.hit_rate()*100:.0f}%")
    print(f"  cost on HUD-rerender frames: mean={ms[sp[:,4]>0].mean() if hud_frames else 0:.2f}ms "
          f"vs no-HUD: mean={ms[sp[:,4]==0].mean():.2f}ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
