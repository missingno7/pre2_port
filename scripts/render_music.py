"""Render PRE2 music to a WAV through the faithful (byte-exact) audio oracle.

    snapshot VM memory
      -> pre2.bridge.audio_commands.capture_module   (recovered command layer)
      -> FaithfulBackend                              (recovered tracker + mixer)
      -> WAV

The module is captured as a neutral asset and played **from the top** with no VM and no
Sound Blaster, reproducing the original byte-exact 8-bit / ~8.4 kHz output (then band-limit
resampled to 44.1 kHz for the WAV). This is the offline oracle/debug tool; the live, modern
audio is the SDL_mixer path (``play.py --view --audio enhanced``).

Run:  python scripts/render_music.py <snapshot_dir> [--seconds N] [--out file.wav]
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

from pre2.audio.assets import SOURCE_RATE
from pre2.audio.faithful_backend import FaithfulBackend
from pre2.bridge import audio_commands as AC
from pre2.runtime import load_pre2_snapshot

OUT_RATE = 44100


def render(snap_dir: Path, seconds: float, out: Path) -> int:
    from scipy.signal import resample_poly

    rt = load_pre2_snapshot(ROOT / "assets" / "pre2.exe", snap_dir,
                            game_root=ROOT / "assets", native_replacements=False)
    module = AC.capture_module(rt.cpu.mem)
    if module.song_length <= 0 or not module.patterns:
        print("no module loaded in this snapshot (need one captured while music is playing)")
        return 2

    fb = FaithfulBackend()
    fb.start_module(module)
    src_rate = module.source_rate or SOURCE_RATE
    pcm8 = fb.render(int(seconds * src_rate / 168) + 1)
    # 8-bit unsigned -> signed (no DC removal, like DOSBox), polyphase to 44.1 kHz mono.
    x = (np.frombuffer(bytes(pcm8), dtype=np.uint8).astype(np.float64) - 128.0) * 256.0
    out_i16 = np.clip(resample_poly(x, OUT_RATE, src_rate), -32768, 32767).astype(np.int16)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(OUT_RATE)
        w.writeframes(np.ascontiguousarray(out_i16).tobytes())
    print(f"[faithful] order={list(module.order[:module.song_length + 1])} -> {len(out_i16)} "
          f"frames @ {OUT_RATE}/16-bit mono ({len(out_i16) / OUT_RATE:.1f}s); wrote {out}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("snapshot", type=Path)
    p.add_argument("--seconds", type=float, default=12.0)
    p.add_argument("--out", type=Path, default=None)
    args = p.parse_args()
    return render(args.snapshot, args.seconds, args.out or ROOT / "artifacts" / "pre2_music_faithful.wav")


if __name__ == "__main__":
    raise SystemExit(main())
