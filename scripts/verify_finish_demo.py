"""Verify a FULL-PLAYTHROUGH game-tick demo against the recorded (pure-ASM) oracle, CONTINUING across every
level-end / respawn / cave-teleport / game-over transition instead of stopping at the first one.

The standard verify_native stops at the first transition (it is a single-level completeness check). A
finish-the-game demo spans many levels + the final-boss finale + the credits/end + the restart, so this
runs the native transition TAILS in place and keeps comparing the recorded per-tick digest — reporting the
stage boundaries it crosses and the FIRST tick that truly diverges or gaps.

    python scripts/verify_finish_demo.py <demo_dir> [max_ticks]
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))

from pre2.gaps import (Pre2CaveTeleport, Pre2GameComplete, Pre2GameOverTransition, Pre2HybridGap,
                       Pre2LevelEndTransition, Pre2RespawnTransition)
from pre2.native.game_tick_demo import GameTickDemo, _inject, gameplay_digest
from pre2.native.level_state import native_4f6c, native_5063, native_level_end
from pre2.native.loop import native_cave_teleport, native_gameplay_frame
from pre2.native.runtime import native_exit_anim
from pre2.native.state import NativeGameState
from pre2.native.vga import NativeVGA

DS = 0x1A0F << 4


def main() -> int:
    demo_dir = sys.argv[1] if len(sys.argv) > 1 else "artifacts/demo_pre2_finish_game_norepl_20260703_165400"
    tick_file = ROOT / demo_dir / "game_tick_demo.bin"
    if not tick_file.exists():
        print(f"no {tick_file} — run verify_native_tick_demo.py {demo_dir} first (records the oracle)")
        return 2
    gtd = GameTickDemo.load(tick_file)
    max_ticks = int(sys.argv[2]) if len(sys.argv) > 2 else gtd.n_ticks
    print(f"loaded {gtd.n_ticks} recorded ticks; verifying native across ALL transitions ...")

    gr = str(ROOT / "assets")
    state = NativeGameState(bytearray(gtd.seed))
    stage_start = 0
    level = state.data[DS + 0x2D8A]
    print(f"  stage: level {level} @ tick 0")

    dos = NativeVGA()
    i = 0
    while i < min(max_ticks, gtd.n_ticks):
        _inject(state, gtd.keys[i], gtd.idle[i] if i < len(gtd.idle) else None)
        try:
            native_gameplay_frame(state)
        except Pre2CaveTeleport as tp:
            # cave-teleport is INLINE (the pan/reveal runs within the frame, which still reaches 026D) -> the
            # trigger frame IS tick i, so fall through to the digest check.
            for _ in native_cave_teleport(state, tp.si):
                pass
        except Pre2RespawnTransition:
            # 4F6C returns clc (no carry) and the loop continues to 026D -> the death-bounce is one tick i.
            for _ in native_4f6c(state):
                pass
        except Pre2LevelEndTransition:
            # 4F65 returns CARRY -> main 0x12f (the level change), so the level-end TRIGGER frame never reaches
            # 026D = it is NOT a recorded tick. The VM runs the tally cutscene (score count-up, off-loop) + loads
            # the next level + its FIRST gameplay frame — and THAT first frame is tick i. Reproduce it: the tally
            # count-up (native_exit_anim STATE mutations), then native_level_end (load), then the first new-level
            # frame with keys[i]. (native_exit_anim is the same state the real play_native between_levels runs.)
            before = state.data[DS + 0x2D8A]
            for _ in native_exit_anim(state, dos, 0, game_root=gr, state_only=True):
                pass
            native_level_end(state, game_root=gr)
            after = state.data[DS + 0x2D8A]
            print(f"  LEVEL-END {before}->{after} @ tick {i}  ({i - stage_start} ticks in the prior stage)")
            stage_start = i
            _inject(state, gtd.keys[i], gtd.idle[i] if i < len(gtd.idle) else None)
            try:
                native_gameplay_frame(state)                        # the first new-level frame IS tick i
            except Pre2HybridGap as e:
                print(f"  DIVERGED: first {after:#x} frame (tick {i}) raised {type(e).__name__}: {str(e)[:90]}")
                return 1
        except Pre2GameOverTransition:
            for _ in native_5063(state):
                pass
            _inject(state, gtd.keys[i], gtd.idle[i] if i < len(gtd.idle) else None)
            native_gameplay_frame(state)                            # game-over restart's first frame = tick i
            stage_start = i
        except Pre2GameComplete:
            print(f"  GAME-COMPLETE @ tick {i}: native reached THE END (level 0xE cleared) "
                  f"after {i - stage_start + 1} ticks in the final stage")
            print(f"  PASS: native reproduced the whole run to the ending screen")
            return 0
        except Pre2HybridGap as e:
            print(f"  DIVERGED: gap at tick {i} (in level {state.data[DS+0x2D8A]}, "
                  f"{i - stage_start} ticks into the stage): {str(e)[:110]}")
            return 1
        except Exception as e:                                      # noqa: BLE001
            import traceback
            traceback.print_exc()
            print(f"  DIVERGED: tick {i} raised {type(e).__name__}: {str(e)[:100]}")
            return 1

        got = gameplay_digest(state.data[DS:DS + 0x10000])
        if got != gtd.digests[i]:
            lvl = state.data[DS + 0x2D8A]
            print(f"  DIVERGED: tick {i} (level {lvl}, {i - stage_start} into stage) digest "
                  f"{got[:12]} != recorded {gtd.digests[i][:12]}")
            return 1
        if i % 200 == 0:
            print(f"    ... tick {i} OK (level {state.data[DS+0x2D8A]})")
        i += 1

    print(f"  PASS: native reproduced all {i} ticks across every stage to level {state.data[DS+0x2D8A]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
