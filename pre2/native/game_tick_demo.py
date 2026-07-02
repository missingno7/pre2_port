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

from pre2.checkpoints.common import (Pre2CaveTeleport, Pre2HybridGap, Pre2LevelEndTransition,
                                     Pre2RespawnTransition)
from pre2.native.level_state import native_4f6c, native_level_end
from pre2.native.loop import native_cave_teleport, native_gameplay_frame
from pre2.native.state import NativeGameState
# Reuse the forward oracle's tick seams + the gameplay/render boundary (single source of truth):
from pre2.probes.probe_native_frame import DECODE, DS_BASE, FRAME_TOP, GAP_SITE, KBD, _SLOT5_PAGE
from pre2.probes.probe_native_forward import _FWD_EXCL

DS = 0x1A0F


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
    return hashlib.sha1(buf).hexdigest()


@dataclass
class GameTickDemo:
    """A recording keyed to game ticks. ``seed`` is the VM's full memory image at the first gameplay
    ``FRAME_TOP`` (the native bootstrap); each tick is the 21 sampled keys + the post-tick gameplay digest."""
    seed: bytes                                  # VM memory at the first gameplay FRAME_TOP (native seed)
    keys: list[bytes] = field(default_factory=list)     # per tick: bytes(len(KBD)) in KBD order
    digests: list[str] = field(default_factory=list)    # per tick: gameplay_digest AFTER the tick

    @property
    def n_ticks(self) -> int:
        return len(self.keys)

    # --- on-disk format (a single file, conventionally <input_demo_dir>/game_tick_demo.bin) --------------
    _MAGIC = b"PRE2GTD1"

    def save(self, path) -> None:
        """Serialize: magic, u32 zlib(seed) length + payload, u32 n_ticks, u8 key-record length, the raw key
        records, then the raw 20-byte SHA1 digests. Compact (the 1 MB seed compresses well) and stdlib-only."""
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
        with open(path, "wb") as f:
            f.write(blob)

    @classmethod
    def load(cls, path) -> "GameTickDemo":
        import struct
        import zlib
        raw = open(path, "rb").read()
        if raw[:8] != cls._MAGIC:
            raise ValueError(f"{path}: not a game-tick demo (bad magic)")
        off = 8
        (zlen,) = struct.unpack_from("<I", raw, off); off += 4
        seed = zlib.decompress(raw[off:off + zlen]); off += zlen
        n, klen = struct.unpack_from("<IB", raw, off); off += 5
        keys = [bytes(raw[off + i * klen:off + (i + 1) * klen]) for i in range(n)]
        off += n * klen
        digests = [raw[off + i * 20:off + (i + 1) * 20].hex() for i in range(n)]
        return cls(seed=seed, keys=keys, digests=digests)


def record_from_vm(rt, *, advance_one_frame, max_ticks: int = 100_000) -> GameTickDemo:
    """Drive an already-loaded VM ``rt`` and capture the game-tick timeline.

    ``advance_one_frame()`` is a no-arg callback that advances the VM one present-frame (the caller owns the
    demo/input pacing — e.g. ``play._advance_demo_frame`` over an old input demo, or a live --view step). We
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
                rec["keys"] = bytes(mem.data[DS_BASE + o] for o in KBD)
            elif ip == GAP_SITE and rec["keys"] is not None:
                out.keys.append(rec["keys"])
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


def _inject(state: NativeGameState, keys: bytes) -> None:
    for o, v in zip(KBD, keys):
        state.data[DS_BASE + o] = v


def verify_native(demo: GameTickDemo, *, game_root: str) -> tuple[int, str | None]:
    """Replay the demo on the VM-less native core and check it reproduces the gameplay digest every tick.

    Returns ``(ticks_matched, divergence)`` — ``divergence`` is ``None`` when all ticks matched, else a short
    description of the first tick whose native gameplay state differs from the recording. The native core
    steps one tick per ``native_gameplay_frame`` (a respawn raises ``Pre2RespawnTransition`` and plays out over
    its rendered bounce via ``native_4f6c``, exactly as the standalone runner does)."""
    state = NativeGameState(bytearray(demo.seed))
    for i, (keys, want) in enumerate(zip(demo.keys, demo.digests)):
        _inject(state, keys)
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
            except Exception as e:                                  # noqa: BLE001
                return i, f"tick {i}: respawn tail raised {type(e).__name__}: {str(e)[:80]}"
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
