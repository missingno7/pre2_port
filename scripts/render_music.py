"""Render PRE2 music to a WAV through the semantic-event audio pipeline.

Demonstrates the two-layer architecture end to end:

    snapshot VM memory
      -> pre2.bridge.audio_commands.capture_module        (recovered command layer)
      -> events.StartSong(module)                          (semantic event)
      -> FaithfulBackend | EnhancedBackend                 (interchangeable backends)
      -> WAV

The module is captured as a neutral asset and played **from the top** with no VM and
no Sound Blaster. ``--backend faithful`` reproduces the byte-exact 8-bit/8.4 kHz output
(then band-limit-resampled to 44.1 kHz for the WAV); ``--backend enhanced`` mixes in
float32 at 44.1 kHz directly (HQ resampling, no 8-bit wrap, no DMA/block constraints).

Run:  python scripts/render_music.py <snapshot_dir> [--backend enhanced|faithful]
                                      [--seconds N] [--out file.wav]
"""
from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from pre2.audio.enhanced_backend import OUT_RATE, EnhancedBackend
from pre2.audio.events import StartSong
from pre2.audio.faithful_backend import FaithfulBackend
from pre2.audio.assets import SOURCE_RATE
from pre2.bridge import audio_commands as AC
from pre2.runtime import load_pre2_snapshot


def _write_wav(path: Path, samples_i16: np.ndarray, rate: int) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(samples_i16.tobytes())


def render(snap_dir: Path, seconds: float, out: Path, backend: str) -> int:
    rt = load_pre2_snapshot(ROOT / "assets" / "pre2.exe", snap_dir,
                            game_root=ROOT / "assets", native_replacements=False)
    module = AC.capture_module(rt.cpu.mem)
    if module.song_length <= 0 or not module.patterns:
        print("no module loaded in this snapshot (need one captured while music is playing)")
        return 2

    if backend == "enhanced":
        eb = EnhancedBackend()
        eb.handle(StartSong(module=module))
        y = eb.render(int(seconds * OUT_RATE))
        out_i16 = np.clip(y * 32767.0, -32768, 32767).astype(np.int16)
    else:
        from scipy.signal import resample_poly
        fb = FaithfulBackend()
        fb.handle(StartSong(module=module))
        src_rate = module.source_rate or SOURCE_RATE
        n_blocks = int(seconds * src_rate / 168) + 1
        pcm8 = fb.render(n_blocks)
        # 8-bit unsigned -> signed (no DC removal, like DOSBox), polyphase to 44.1k
        x = (np.frombuffer(bytes(pcm8), dtype=np.uint8).astype(np.float64) - 128.0) * 256.0
        out_i16 = np.clip(resample_poly(x, OUT_RATE, src_rate), -32768, 32767).astype(np.int16)

    _write_wav(out, out_i16, OUT_RATE)
    print(f"[{backend}] order={list(module.order[:module.song_length + 1])} -> "
          f"{len(out_i16)} frames @ {OUT_RATE}/16-bit ({len(out_i16) / OUT_RATE:.1f}s); wrote {out}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("snapshot", type=Path)
    p.add_argument("--backend", choices=("enhanced", "faithful"), default="enhanced")
    p.add_argument("--seconds", type=float, default=12.0)
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()
    out = args.out or ROOT / "artifacts" / f"pre2_music_{args.backend}.wav"
    return render(args.snapshot, args.seconds, out, args.backend)


if __name__ == "__main__":
    raise SystemExit(main())
