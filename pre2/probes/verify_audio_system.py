"""In-VM lockstep verify of the native audio engine ``AudioSystem`` (audio Layer 5).

Proves the recovered, VM-independent :class:`pre2.recovered.audio_system.AudioSystem`
reproduces the original audio ISR's PCM **byte-exact**: it drives a music snapshot, and at
each sequencer tick (``1030:227C``) it captures the full :class:`AudioState` from memory,
runs one detached ``next_block``, lets the ASM ISR mix that same block, and diffs the 168
bytes. This is the audio counterpart of ``verify_render_frame.py`` — the source-of-truth
check the high-quality (ear-candy) mixer is validated against.

Needs a snapshot with music playing (a legally-owned GOG copy; gitignored).

Run:  python -m pre2.probes.verify_audio_system [artifacts/snapshot_dir]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import play
from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook
from dos_re.runtime import enable_sound_blaster
from pre2.bridge import audio as _a
from pre2.bridge.audio_system import capture_audio_state
from pre2.recovered.audio_system import AudioSystem
from pre2.runtime import load_pre2_snapshot

CS = 0x1030
TRACKER = 0x227C       # sequencer tick (once per audio block)
MIX_CH = 0x218F        # per-channel mixer (3-4 calls per block)
DEFAULT_SNAP = ROOT / "artifacts" / "snapshot_pre2_palette_fade_20260622_021225"
DET = 450000.0
WARMUP_TICKS = 50          # let the SB double-buffer reach steady state after load
LIMIT = 40
SFX_ACTIVE = 0x1006


def main() -> int:
    snap = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SNAP
    if not snap.exists():
        print(f"snapshot not found: {snap} (needs a music snapshot under artifacts/)")
        return 2
    rt = load_pre2_snapshot(ROOT / "assets" / "pre2.exe", snap,
                            game_root=ROOT / "assets", native_replacements=True)
    cpu = rt.cpu
    cpu.trace_enabled = False
    sb = enable_sound_blaster(rt)
    det_now = lambda: cpu.instruction_count / DET  # noqa: E731
    sb.clock = det_now
    rt.dos.time_source = det_now

    state = {"ticks": 0, "pending": None, "mixes": 0, "verified": 0}
    diverged: list[str] = []

    def at_tracker(c):
        state["ticks"] += 1
        if state["ticks"] > WARMUP_TICKS and state["pending"] is None and not diverged:
            # capture the full audio state, run one detached block
            st = capture_audio_state(c.mem)
            state["pending"] = bytes(AudioSystem(st).next_block())
            state["mixes"] = 0
            # the ISR mixes ch0-2 always + ch3 only when no SFX is active
            state["expect"] = 3 if _a._rw(c.mem, _a.DATA_SEG, SFX_ACTIVE) != 0 else 4
        interpret_current_instruction_without_hook(c)

    def at_mix(c):
        state["mixes"] += 1
        interpret_current_instruction_without_hook(c)

    cpu.replacement_hooks[(CS, TRACKER)] = at_tracker
    cpu.hook_names[(CS, TRACKER)] = "audio_sys_capture"
    cpu.replacement_hooks[(CS, MIX_CH)] = at_mix
    cpu.hook_names[(CS, MIX_CH)] = "audio_sys_mixcount"

    ts = {"next": 0.0}
    guard = 0
    while state["verified"] < LIMIT and not diverged and guard < 6000:
        play._pump_and_step(rt, now=det_now(), pic=rt.dos.pic, sound_blaster=sb,
                            timer_irq=True, input_irq_steps=2_000_000, tick_state=ts, n_steps=2000)
        guard += 1
        if state["pending"] is not None and state["mixes"] >= state["expect"]:
            # the ISR has mixed this block's channels into the fill buffer
            vm_block = bytes(_a.read_block(rt.cpu.mem))
            my = state["pending"]
            nbad = sum(1 for i in range(168) if my[i] != vm_block[i])
            state["verified"] += 1
            if nbad:
                i = next(k for k in range(168) if my[k] != vm_block[k])
                diverged.append(f"block#{state['verified']} @{i}: my={my[i]:02X} vm={vm_block[i]:02X}")
            state["pending"] = None

    print(f"audio blocks verified={state['verified']}")
    print(f"divergences={diverged[:8]}")
    ok = not diverged and state["verified"] > 0
    print("AUDIO_SYSTEM LOCKSTEP:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
