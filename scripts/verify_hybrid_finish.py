"""Phase 5 proof: the WHOLE game lifecycle — a full playthrough across EVERY level-end / respawn / teleport /
game-over transition — runs gameplay on the named field store, byte-exact vs the pure-ASM VM oracle.

This is verify_finish_demo (which compares each tick against the recorded VM digest across all transitions)
but with the state on the HYBRID field store. The umbilical pattern: gameplay runs on the field store; at a
transition (native_level_end / exit-anim / respawn / teleport — raw-.data load code) MATERIALISE the field
store into the image, run the transition, then RE-SEED the field store from the new image. Each tick's digest
is checked against the original after materialising. If it reproduces the whole run, the field store is a
sufficient state of record for the ENTIRE product lifecycle — the byte image (and the original's memory
format) is only ever a load/serialise buffer, i.e. a fully detachable umbilical cord.

    python scripts/verify_hybrid_finish.py [demo_dir] [max_ticks]
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts")); sys.path.insert(0, str(ROOT / "dos_re"))

from pre2.gaps import (Pre2CaveTeleport, Pre2GameComplete, Pre2GameOverTransition, Pre2HybridGap,
                       Pre2LevelEndTransition, Pre2RespawnTransition)
from pre2.native.game_tick_demo import GameTickDemo, _inject, gameplay_digest
from pre2.native.level_state import native_4f6c, native_5063, native_level_end
from pre2.native.loop import native_cave_teleport, native_gameplay_frame
from pre2.native.runtime import native_exit_anim
from pre2.native.state import NativeGameState
from pre2.native.vga import NativeVGA
from pre2.native.field_runtime import enter_image_mode, materialize, to_field_store

DS = 0x1A0F << 4


def main() -> int:
    demo_dir = sys.argv[1] if len(sys.argv) > 1 else "artifacts/demo_pre2_finish_game_norepl_20260703_165400"
    tick_file = ROOT / demo_dir / "game_tick_demo.bin"
    if not tick_file.exists():
        print(f"no {tick_file} — run verify_native_tick_demo.py {demo_dir} first")
        return 2
    gtd = GameTickDemo.load(tick_file)
    max_ticks = int(sys.argv[2]) if len(sys.argv) > 2 else gtd.n_ticks
    print(f"loaded {gtd.n_ticks} recorded ticks; verifying the FIELD-STORE run across ALL transitions ...")

    gr = str(ROOT / "assets")
    state = NativeGameState(bytearray(gtd.seed))
    to_field_store(state)
    stage_start = 0
    dos = NativeVGA()
    i = 0
    while i < min(max_ticks, gtd.n_ticks):
        _inject(state, gtd.keys[i], gtd.idle[i] if i < len(gtd.idle) else None)
        try:
            native_gameplay_frame(state)                            # on the field store
        except Pre2CaveTeleport as tp:
            enter_image_mode(state)
            for _ in native_cave_teleport(state, tp.si):
                pass
            to_field_store(state)
        except Pre2RespawnTransition:
            enter_image_mode(state)
            for _ in native_4f6c(state):
                pass
            to_field_store(state)
        except Pre2LevelEndTransition:
            enter_image_mode(state)                                   # fold named state into the image for the load
            for _ in native_exit_anim(state, dos, 0, game_root=gr, state_only=True):
                pass
            before = state.data[DS + 0x2D8A]
            native_level_end(state, game_root=gr)                   # loads the next level (raw-.data writes)
            after = state.data[DS + 0x2D8A]
            to_field_store(state)                                          # named state picks up the freshly-loaded level
            print(f"  LEVEL-END {before}->{after} @ tick {i}  ({i - stage_start} ticks in the prior stage)")
            stage_start = i
            _inject(state, gtd.keys[i], gtd.idle[i] if i < len(gtd.idle) else None)
            try:
                native_gameplay_frame(state)                        # the first new-level frame IS tick i
            except Pre2HybridGap as e:
                print(f"  DIVERGED: first {after:#x} frame (tick {i}) raised {type(e).__name__}: {str(e)[:90]}")
                return 1
        except Pre2GameOverTransition:
            enter_image_mode(state)
            for _ in native_5063(state):
                pass
            to_field_store(state)
            _inject(state, gtd.keys[i], gtd.idle[i] if i < len(gtd.idle) else None)
            native_gameplay_frame(state)
            stage_start = i
        except Pre2GameComplete:
            print(f"  GAME-COMPLETE @ tick {i}: the field-store run reached THE END after "
                  f"{i - stage_start + 1} ticks in the final stage")
            print("  PASS: the named field store reproduced the WHOLE playthrough to the ending.")
            return 0
        except Pre2HybridGap as e:
            print(f"  DIVERGED: gap at tick {i} (level {state.data[DS+0x2D8A]}): {str(e)[:110]}")
            return 1

        materialize(state)                                            # materialise before the digest read
        got = gameplay_digest(state.data[DS:DS + 0x10000])
        if got != gtd.digests[i]:
            lvl = state.data[DS + 0x2D8A]
            print(f"  DIVERGED: tick {i} (level {lvl}) digest {got[:12]} != VM {gtd.digests[i][:12]}")
            return 1
        if i % 200 == 0:
            print(f"    ... tick {i} OK (level {state.data[DS+0x2D8A]}) — on the field store")
        i += 1

    print(f"  PASS: the field store reproduced all {i} ticks across every stage (level {state.data[DS+0x2D8A]}).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
