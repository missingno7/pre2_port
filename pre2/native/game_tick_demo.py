"""Game-tick-synced demo — PRE2-specific, mode-independent verification of the VM-less native core.

The legacy input demo (``dos_re.input_demo``) keys input to PRESENT frames and advances the VM by a fixed
INSTRUCTION budget per frame. That budget is mode-dependent: a recovered hook runs far fewer emulated
instructions than the ASM it replaces, so the same demo advances the game by a different amount in
pure-ASM / hybrid / VM-less-native — a demo recorded in one mode desyncs in another, and the native core has
no instruction count at all. (Proven: a ``--no-replacements`` recording replayed in the hybrid drifts, while
``--full-verify`` shows every gameplay hook byte-exact — the divergence is purely the clock, not the logic.)

This demo is keyed to the GAME TICK instead: one main-loop iteration (1030:021A..0270, ``[0x6BD5]++``). Per
tick it stores the keyboard the game samples at DC1 (the 21 key-table bytes ``[0x27F4+scancode]`` DC1 reads)
plus a digest of the GAMEPLAY state after the tick. Replay steps exactly one tick and injects those keys, so
it runs IDENTICALLY in every mode — and the VM-less native core (``native_gameplay_frame`` == one tick)
consumes it directly. If native reproduces the digest at EVERY tick, the VM-less game is proven to reproduce
the VM byte-for-byte over the whole recording.

The digest covers the gameplay DGROUP only: render-only state (display pages, scroll/dirty-grid, draw-lists,
HUD), input plumbing (the demo-RLE cursor) and async audio (the SB ISR, which native does not run) are
excluded — the same boundary the forward oracle uses, i.e. exactly the state the gameplay tick OWNS.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from pre2.gaps import (Pre2CaveTeleport, Pre2CheatCredits, Pre2GameComplete, Pre2GameOverTransition, Pre2HybridGap,
                                     Pre2LevelEndTransition, Pre2RespawnTransition)
from pre2.native.level_init import native_level_init
from pre2.native.level_state import native_4f6c, native_5063, native_level_end
from pre2.native.loop import native_cave_teleport, native_gameplay_frame
from pre2.native.state import NativeGameState
# Reuse the forward oracle's tick seams + the gameplay/render boundary (single source of truth):
from pre2.recovered.input_decode import _KBD_SOURCES
from pre2.native.seams import (DECODE, DS_BASE, FRAME_TOP, GAP_SITE, KBD, KEY_SAMPLE,
                               _FWD_EXCL, _SLOT5_PAGE, _SLOT_BASE, _SLOT_STRIDE)
from pre2.views.dgroup_view import PlayerGlobals

DS = 0x1A0F
FIDGET_READ = 0x5DCC   # [asm 5DCC] the idle-fidget selector's `mov ax,[0x27F0]` (the PIT-timer read point)


def gameplay_digest(dgroup: bytes | bytearray) -> str:
    """SHA1 of the 64 KB DGROUP with the non-gameplay state neutralised — the fingerprint two runs must share
    if they computed the same gameplay tick. Zeroes the render/input-plumbing/audio offsets (``_FWD_EXCL``) and
    masks the slot-5 page bit (``& 0x9F``) exactly as the forward-oracle byte compare does, so a digest match
    means the same gameplay state by the same definition the lockstep oracle proves byte-exact."""
    buf = bytearray(dgroup[:0x10000])
    for o in _FWD_EXCL:
        if o < 0x10000:
            buf[o] = 0
    for o in _SLOT5_PAGE:
        if o < 0x10000:
            buf[o] &= 0x9F
    # An EMPTY render slot ([+4:6]==0xFFFF) keeps STALE projected X/Y (fields 0-3) — render residue the gameplay
    # tick never reads (native's projection doesn't refresh a freed slot, so its old x/y drifts vs the VM's). The
    # forward-oracle byte compare excludes exactly this (both-empty slots, all but the player 0x4F1C); the digest
    # must use the SAME boundary or it flags a non-divergence. Zero fields 0-3 of THIS dgroup's empty slots — when
    # both runs have the slot empty their zeroed x/y match; a real occupancy change still shows in the id [+4].
    for b in range(_SLOT_BASE, 0x5732, _SLOT_STRIDE):
        if b != 0x4F1C and dgroup[b + 4] == 0xFF and dgroup[b + 5] == 0xFF:
            buf[b] = buf[b + 1] = buf[b + 2] = buf[b + 3] = 0
    return hashlib.sha1(buf).hexdigest()


@dataclass
class GameTickDemo:
    """A recording keyed to game ticks. ``seed`` is the VM's full memory image at the first gameplay
    ``FRAME_TOP`` (the native bootstrap); each tick is the 21 sampled keys + the post-tick gameplay digest."""
    seed: bytes                                  # VM memory at the first gameplay FRAME_TOP (native seed)
    keys: list[bytes] = field(default_factory=list)     # per tick: bytes(len(KBD)) in KBD order
    digests: list[str] = field(default_factory=list)    # per tick: gameplay_digest AFTER the tick
    idle: list[int] = field(default_factory=list)       # per tick: the VM's [0x27F0] idle-timer word at DECODE —
    #   the PIT-driven counter the idle-fidget selector (5DC9: [0x27F0]&0x1FF) reads. It is INSTRUCTION-COUNT
    #   driven (4..11 PIT ticks/frame), so no VM-less advance is byte-exact; inject the VM's value per tick so the
    #   idle pose reproduces exactly (else a stationary player fidgets to a different frame -> [0x4F20/28/2C] drift).

    @property
    def n_ticks(self) -> int:
        return len(self.keys)

    # --- on-disk format (a single file, conventionally <input_demo_dir>/game_tick_demo.bin) --------------
    _MAGIC = b"PRE2GTD2"          # v2 adds the per-tick idle-timer [0x27F0] words after the digests
    _MAGIC_V1 = b"PRE2GTD1"       # v1 (no idle timeline) — still loadable; idle defaults to 0 (no injection)

    def save(self, path) -> None:
        """Serialize: magic, u32 zlib(seed) length + payload, u32 n_ticks, u8 key-record length, the raw key
        records, the raw 20-byte SHA1 digests, then the per-tick u16 idle-timer words. Compact + stdlib-only."""
        import struct
        import zlib
        zseed = zlib.compress(bytes(self.seed), 6)
        klen = len(self.keys[0]) if self.keys else len(KBD)
        blob = bytearray()
        blob += self._MAGIC
        blob += struct.pack("<I", len(zseed)) + zseed
        blob += struct.pack("<IB", self.n_ticks, klen)
        for k in self.keys:
            blob += k
        for d in self.digests:
            blob += bytes.fromhex(d)
        for v in self.idle:
            blob += struct.pack("<H", v & 0xFFFF)
        with open(path, "wb") as f:
            f.write(blob)

    @classmethod
    def load(cls, path) -> "GameTickDemo":
        import struct
        import zlib
        raw = open(path, "rb").read()
        v2 = raw[:8] == cls._MAGIC
        if not v2 and raw[:8] != cls._MAGIC_V1:
            raise ValueError(f"{path}: not a game-tick demo (bad magic)")
        off = 8
        (zlen,) = struct.unpack_from("<I", raw, off); off += 4
        seed = zlib.decompress(raw[off:off + zlen]); off += zlen
        n, klen = struct.unpack_from("<IB", raw, off); off += 5
        keys = [bytes(raw[off + i * klen:off + (i + 1) * klen]) for i in range(n)]
        off += n * klen
        digests = [raw[off + i * 20:off + (i + 1) * 20].hex() for i in range(n)]
        off += n * 20
        idle = ([struct.unpack_from("<H", raw, off + i * 2)[0] for i in range(n)] if v2 else [0] * n)
        return cls(seed=seed, keys=keys, digests=digests, idle=idle)


def record_from_vm(rt, *, advance_one_frame, max_ticks: int = 100_000) -> GameTickDemo:
    """Drive an already-loaded VM ``rt`` and capture the game-tick timeline.

    ``advance_one_frame()`` is a no-arg callback that advances the VM one present-frame (the caller owns the
    demo/input pacing — e.g. ``play._advance_demo_frame`` over an old input demo, or a live-view step). We
    hook ``cpu.step`` only to observe the tick seams: snapshot the native seed at the first ``FRAME_TOP``,
    capture the sampled keys at ``DECODE``, and the post-tick gameplay digest at ``GAP_SITE``. Audio is left
    to the caller (record without the SB so the audio state stays static — it is excluded from the digest
    anyway, and native does not run the SB)."""
    cpu = rt.cpu
    mem = cpu.mem
    rec: dict = {"seed": None, "keys": None, "out": GameTickDemo(seed=b"")}
    out = rec["out"]
    orig = cpu.step

    def sstep():
        s = cpu.s
        if (s.cs & 0xFFFF) == 0x1030 and (s.ds & 0xFFFF) == DS:
            ip = s.ip & 0xFFFF
            if ip == FRAME_TOP and rec["seed"] is None:
                rec["seed"] = bytes(mem.data)                      # native bootstrap (first gameplay frame)
                out.seed = rec["seed"]
            elif ip == DECODE and rec["seed"] is not None:
                # Capture the sampled keys at DC1's ENTRY (0DC1) as a base — always reached, and in the HYBRID
                # oracle DC1 is a replaced hook (a single atomic step, so its cell reads cannot race an INT 09)
                # whose read IS at 0DC1. The ASM path overwrites this at KEY_SAMPLE below.
                rec["keys"] = bytes(mem.data[DS_BASE + o] for o in KBD)
                rec["idle"] = mem.data[DS_BASE + 0x27F0] | (mem.data[DS_BASE + 0x27F1] << 8)  # the fidget timer
            elif ip == KEY_SAMPLE and rec["seed"] is not None:
                # [asm 0F0A] the ASM (pure/safe) path: DC1's flag ORs [asm 0EA4..0F06] just ran, so the six
                # computed flags [0x27E8..ED] are exactly what THIS tick consumed. Capturing the raw [0x28xx]
                # cells here instead races INT 09: a key make/break delivered between a cell's OR-read and this
                # sample point changes the cell AFTER its value was consumed (observed: a mid-window RELEASE
                # made the VM move with [0x27EA]=0xFF while the captured cell — hence native's re-decode — said
                # 0; safe demo 230900 tick 1858). So SYNTHESIZE the cell image from the consumed flags (first
                # source cell of each flag = the flag value): KBD is exactly the union of the six flags' source
                # cells, so the whole recorded input influence flows through these ORs and the synthesis is
                # faithful by construction — the same capture-at-the-consumption-point rule as FIDGET_READ.
                cells = dict.fromkeys(KBD, 0)
                for flag_off, srcs in _KBD_SOURCES.items():
                    cells[srcs[0]] = mem.data[DS_BASE + flag_off]
                rec["keys"] = bytes(cells[o] for o in KBD)
                rec["idle"] = mem.data[DS_BASE + 0x27F0] | (mem.data[DS_BASE + 0x27F1] << 8)
            elif ip == FIDGET_READ and rec["keys"] is not None:
                # [asm 5DCC] the idle-fidget selector reads [0x27F0] HERE, not at DECODE — and the free-running
                # timer advances within the frame (PIT-driven, not VM-less-reproducible), so the value the fidget
                # sees differs from DECODE's by the mid-frame increment. Capture it AT the read so native's inject
                # gives its VM-less fidget the exact key the VM used (else the low-9-bit key can wrap -> wrong pose).
                rec["idle"] = mem.data[DS_BASE + 0x27F0] | (mem.data[DS_BASE + 0x27F1] << 8)
            elif ip == GAP_SITE and rec["keys"] is not None:
                out.keys.append(rec["keys"])
                out.idle.append(rec["idle"])
                out.digests.append(gameplay_digest(mem.data[DS_BASE:DS_BASE + 0x10000]))
                rec["keys"] = None
        orig()

    cpu.step = sstep
    try:
        while out.n_ticks < max_ticks and advance_one_frame():
            pass
    finally:
        cpu.step = orig
    return out


def _inject(state: NativeGameState, keys: bytes, idle: int | None = None) -> None:
    # An EXTERNAL input injection must reach every reader regardless of the backend routing (the idle timer
    # 0x27F0 and some key cells are read both through the seam and via raw .data). Write BOTH the backend
    # (so a hybrid field store sees it) and the raw image (so non-routed readers see it).
    for o, v in zip(KBD, keys):
        state.wb(o, v)
        state.data[DS_BASE + o] = v
    if idle is not None:                              # inject the VM's PIT idle-timer so the idle-fidget selector
        PlayerGlobals(state).idle_clock = idle             # (5DC9: [0x27F0]&0x1FF) picks the SAME pose as the VM — the
        state.data[DS_BASE + 0x27F0] = idle & 0xFF   # counter is not VM-less-reproducible (PIT-driven)
        state.data[DS_BASE + 0x27F1] = (idle >> 8) & 0xFF


def verify_native(demo: GameTickDemo, *, game_root: str) -> tuple[int, str | None]:
    """Replay the demo on the VM-less native core and check it reproduces the gameplay digest every tick.

    Returns ``(ticks_matched, divergence)`` — ``divergence`` is ``None`` when all ticks matched, else a short
    description of the first tick whose native gameplay state differs from the recording. The native core
    steps one tick per ``native_gameplay_frame`` (a respawn raises ``Pre2RespawnTransition`` and plays out over
    its rendered bounce via ``native_4f6c``, exactly as the standalone runner does)."""
    state = NativeGameState(bytearray(demo.seed))
    for i, (keys, want) in enumerate(zip(demo.keys, demo.digests)):
        _inject(state, keys, demo.idle[i] if i < len(demo.idle) else None)
        try:
            native_gameplay_frame(state)
        except Pre2CaveTeleport as tp:
            try:
                for _ in native_cave_teleport(state, tp.si):        # drain the transition (state-only)
                    pass
            except Exception as e:                                  # noqa: BLE001
                return i, f"tick {i}: cave teleport raised {type(e).__name__}: {str(e)[:80]}"
        except Pre2RespawnTransition:
            try:
                for _ in native_4f6c(state):
                    pass
            except Pre2GameOverTransition:
                # a real death during the respawn -> the shared 506c game-over tail (native_4f6c applied the reset,
                # same as native_5063). Same terminal as the [0x6be5]==1 path: the restart is front-end flow.
                return i, (f"GAME-OVER at tick {i}: real death during respawn (4F6C -> 506c); native played the "
                           f"death-bounce + reset to level 1; the restart is main's front-end flow")
            except Exception as e:                                  # noqa: BLE001
                return i, f"tick {i}: respawn tail raised {type(e).__name__}: {str(e)[:80]}"
        except Pre2GameOverTransition:
            try:
                for _ in native_5063(state):                        # death-bounce + reset to level 1 + score 0
                    pass
            except Exception as e:                                  # noqa: BLE001
                return i, f"tick {i}: game-over tail raised {type(e).__name__}: {str(e)[:80]}"
            # The game restarts via main's 0x12f FRONT-END re-entry (447d + 8e45 + the carte scene + level reload),
            # not a plain level load — reproducing it byte-exact is front-end recovery, not gameplay. The gameplay
            # tick compare ends here (native reproduced the whole run up to the death), exactly like LEVEL-END.
            return i, (f"GAME-OVER at tick {i}: native played the death-bounce + reset to level 1 (5063); the "
                       f"restart is main's 0x12f front-end flow (a scene, no gameplay counterpart)")
        except Pre2GameComplete:
            # The player cleared the final level 0xE -> THE END (5034). That is a front-end scene (THEEND.SQZ +
            # the fade/wait), not a gameplay tick, so the tick compare ends here — native reproduced the whole run
            # up to the game's completion.
            return i, (f"GAME-COMPLETE at tick {i}: native reached THE END (level 0xE cleared, 5034); the ending "
                       f"screen is main's 0x12f front-end flow (a scene, no gameplay counterpart)")
        except Pre2CheatCredits:
            # The 247B dev-credits cheat combo fired — a pure overlay SCENE (2505) with no gameplay-tick
            # counterpart, so the tick compare ends here (native reproduced the run up to the combo).
            return i, f"CHEAT-CREDITS at tick {i}: native detected the 247B dev-credits combo (a scene overlay)"
        except Pre2LevelEndTransition:
            before = state.data[DS_BASE + 0x2D8A]
            try:
                native_level_end(state, game_root=game_root)        # advance to + load the next level
            except Exception as e:                                  # noqa: BLE001
                return i, f"tick {i}: level-end tail raised {type(e).__name__}: {str(e)[:80]}"
            after = state.data[DS_BASE + 0x2D8A]
            return i, (f"LEVEL-END at tick {i}: native advanced level {before}->{after} and loaded it (transition "
                       f"ran; tick compare ends here — the VM's exit-anim frames have no native counterpart)")
        except Pre2HybridGap as e:
            return i, f"tick {i}: native hit a gap: {str(e)[:90]}"
        except Exception as e:                                      # noqa: BLE001
            return i, f"tick {i}: native raised {type(e).__name__}: {str(e)[:80]}"
        got = gameplay_digest(state.data[DS_BASE:DS_BASE + 0x10000])
        if got != want:
            return i, f"tick {i}: gameplay digest mismatch (native {got[:12]} != recorded {want[:12]})"
    return len(demo.keys), None
