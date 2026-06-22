"""Render PRE2 music to a high-quality WAV from the native AudioSystem (audio ear-candy).

Demonstrates the layered audio architecture:
  * the **source of truth** is the recovered, byte-exact ``AudioSystem`` (Layer 5) — it runs
    the recovered tracker + mixer with NO VM and NO emulated Sound Blaster;
  * the **ear candy** is a separate output stage: the faithful 8-bit / ~8.4 kHz blocks are
    band-limited resampled (scipy polyphase) to 44.1 kHz / 16-bit, click-free.

Faithful fidelity is proven by ``pre2/probes/verify_audio_system.py`` (40 blocks / 0
divergence vs the original ISR). Here we capture the song state from a snapshot at a clean
sequencer-tick boundary and render forward. NOTE: this offline render plays the music + the
SFX active at capture; game-triggered SFX that fire later cannot be replayed offline (live
integration observes them per block). Use a music snapshot.

Run:  python scripts/render_music.py <snapshot_dir> [seconds] [out.wav]
"""
from __future__ import annotations

import sys
import wave
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import play
from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.runtime import enable_sound_blaster
from pre2.bridge.audio_system import capture_audio_state
from pre2.recovered.audio_system import AudioSystem
from pre2.recovered.mixer import BLOCK_LEN
from pre2.runtime import load_pre2_snapshot

CS = 0x1030
TRACKER = 0x227C
DET = 450000.0
WARMUP_TICKS = 60
OUT_RATE = 44100


def render(snap_dir: Path, seconds: float, out: Path) -> int:
    rt = load_pre2_snapshot(ROOT / "assets" / "pre2.exe", snap_dir,
                            game_root=ROOT / "assets", native_replacements=True)
    cpu = rt.cpu
    cpu.trace_enabled = False
    sb = enable_sound_blaster(rt)
    det_now = lambda: cpu.instruction_count / DET  # noqa: E731
    sb.clock = det_now
    rt.dos.time_source = det_now

    grab = {"state": None, "n": 0}

    def at_tracker(c):
        grab["n"] += 1
        if grab["n"] == WARMUP_TICKS and grab["state"] is None:
            grab["state"] = capture_audio_state(c.mem)
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[(CS, TRACKER)] = at_tracker
    cpu.hook_names[(CS, TRACKER)] = "capture"
    ts = {"next": 0.0}
    guard = 0
    while grab["state"] is None and guard < 4000:
        play._pump_and_step(rt, now=det_now(), pic=rt.dos.pic, sound_blaster=sb,
                            timer_irq=True, input_irq_steps=2_000_000, tick_state=ts, n_steps=4000)
        guard += 1
    if grab["state"] is None:
        print("no music tracker activity in this snapshot (need a snapshot with music playing)")
        return 2

    src_rate = sb.sample_rate or 8403
    n_blocks = int(seconds * src_rate / BLOCK_LEN) + 1
    pcm8 = AudioSystem(grab["state"]).render(n_blocks)            # faithful 8-bit source of truth

    # ear candy: 8-bit unsigned -> signed 16-bit (no DC removal, like DOSBox), polyphase to 44.1k
    x = (np.frombuffer(bytes(pcm8), dtype=np.uint8).astype(np.float64) - 128.0) * 256.0
    y = np.clip(resample_poly(x, OUT_RATE, src_rate), -32768, 32767).astype(np.int16)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(OUT_RATE)
        w.writeframes(y.tobytes())
    print(f"rendered {len(pcm8)} faithful 8-bit bytes @ {src_rate} Hz "
          f"-> {len(y)} frames @ {OUT_RATE}/16-bit ({len(y)/OUT_RATE:.1f}s); wrote {out}")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    snap = Path(sys.argv[1])
    seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 8.0
    out = Path(sys.argv[3]) if len(sys.argv) > 3 else ROOT / "artifacts" / "pre2_music_hq.wav"
    return render(snap, seconds, out)


if __name__ == "__main__":
    raise SystemExit(main())
