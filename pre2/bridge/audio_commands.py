"""Recovered audio **command** layer: VM audio commands -> semantic events.

This is the archaeological bridge between the original DOS audio command routines and
the VM-independent :mod:`pre2.audio.events` stream. It knows the *layout* of the
command interface (where the descriptor table / module header live) but emits only
semantic events; no mixer internals leak past it.

Recovered command roots (GOG build, seg 1030 — see the symbol ledger / memory
``pre2-audio-command-interface``):

* **play_sfx @ 0x0282** (27 call sites): ``dl`` = effect index. The digital path
  (taken when ``cs:[0x1d6c]`` or ``cs:[0x1d6d]`` == 1) reads a 4-byte descriptor
  ``{src, len}`` at ``DS:0x1009 + dl*4`` and streams it from segment ``[0x0b59]``.
  (The non-digital variant ``dl*10 + 0x1037`` drives PC-speaker/notes — unused on
  this SB-digital build, so it is reported but not played.)
* **song loader @ 0x02cc** (8 call sites): parses the "M.K." ProTracker module from
  segment ``[0x0b5e]`` into ``song_length`` ``[0xDC2]`` + the order table ``[0xDC7]``.
* **music-enabled flag**: ``cs:[3]`` bit 0x40 (music ON when the bit is *clear*).
"""
from __future__ import annotations

import glob
import os
import traceback

from pre2.bridge import audio as _a
from pre2.audio.assets import SOURCE_RATE, Module, SampleAsset
from pre2.audio.events import PlaySfx, SetMusicEnabled, StartSong, StopSong
from pre2.codecs.audio import ModModule, load_trk

CODE_SEG = _a.CODE_SEG
DATA_SEG = _a.DATA_SEG

PLAY_SFX = 0x0282          # entry: dl = effect index
SONG_LOADER = 0x02CC       # entry: loads the M.K. module
SFX_TABLE = 0x1009         # DS:0x1009 + dl*4 -> {src word, len word}
SFX_DEV_FLAGS = (0x1D6C, 0x1D6D)   # cs: digital-device-present flags (either == 1)

__all__ = [
    "resolve_sfx", "capture_module", "identify_song", "make_start_song",
    "sfx_enabled", "music_enabled", "install_command_observers",
]


_diag_seen: dict[tuple, int] = {}


def _diag(where: str, exc: BaseException) -> None:
    """Make an observer error VISIBLE (don't silently swallow). Prints the first
    occurrence of each distinct error with a traceback, then counts the rest, so a
    real failure surfaces instead of hiding as silent no-music/no-sfx."""
    key = (where, type(exc).__name__, str(exc)[:80])
    n = _diag_seen.get(key, 0) + 1
    _diag_seen[key] = n
    if n == 1:
        print(f"[audio-obs] ERROR in {where}: {type(exc).__name__}: {exc}", flush=True)
        traceback.print_exc()


def _cs_byte(mem, off: int) -> int:
    return mem.data[((CODE_SEG << 4) + off) & 0xFFFFF]


def sfx_enabled(mem) -> bool:
    """Whether the digital-SFX path is active (a digital device was detected)."""
    return any(_cs_byte(mem, off) == 1 for off in SFX_DEV_FLAGS)


def music_enabled(mem) -> bool:
    """Music ON == cs:[3] bit 0x40 clear (matches :func:`pre2.bridge.audio.music_on`)."""
    return _a.music_on(mem)


# --- PlaySfx (0x0282 digital path) ------------------------------------------------

def resolve_sfx(mem, dl: int, *, volume: int = 0x40, source_rate: int = SOURCE_RATE) -> PlaySfx:
    """Resolve the ``play_sfx(dl)`` digital command into a self-contained event.

    Mirrors 0x2a9: ``bx = dl*4``; ``src = [bx+0x1009]``, ``len = [bx+0x100b]``; the
    sample bytes come from segment ``[0x0b59]:src``."""
    base = SFX_TABLE + (dl & 0xFF) * 4
    src = _a._rw(mem, DATA_SEG, base)
    length = _a._rw(mem, DATA_SEG, base + 2)
    seg = _a._rw(mem, DATA_SEG, _a.SFX_SEG_PTR)
    flat = ((seg << 4) + src) & 0xFFFFF
    pcm = bytes(mem.data[flat:flat + length])
    return PlaySfx(sfx_id=dl & 0xFF, pcm=pcm, volume=volume, source_rate=source_rate)


# --- StartSong (the loaded module) ------------------------------------------------

def capture_module(mem, n_instruments: int = 64) -> Module:
    """Snapshot the currently-loaded module as a neutral :class:`Module` asset."""
    order_table = _a.read_order_table(mem)
    song_length = min(_a.read_song_length(mem), len(order_table) - 1)
    tracker_instr = _a.read_tracker_instruments(mem, n_instruments)
    samples = []
    for i in range(n_instruments):
        instr = _a.read_instrument(mem, i, tracker_instr[i].length)
        loop_rel = (instr.loop_start - instr.ptr_off) & 0xFFFF
        samples.append(SampleAsset(
            pcm=instr.sample, length=tracker_instr[i].length,
            loop_start=loop_rel, loop_len=instr.loop_len,
            default_volume=tracker_instr[i].default_volume,
        ))
    patterns: dict[int, bytes] = {}
    for op in range(song_length + 1):
        pat = order_table[op]
        if pat not in patterns:
            patterns[pat] = _a.read_current_pattern(mem, op)
    return Module(
        order=tuple(order_table), song_length=song_length, patterns=patterns,
        samples=tuple(samples), period_table=tuple(_a.read_period_table(mem)),
        vol_table=_a.read_volume_table(mem), initial_speed=_a.read_playback(mem).speed,
    )


# --- identify which standard .TRK the game just loaded (root StartSong in the asset) ---

_TRK_INDEX: list[tuple[str, ModModule]] | None = None


def _trk_index(assets_dir) -> list[tuple[str, ModModule]]:
    """(filename, parsed module) for every ``.TRK`` asset, parsed once + cached."""
    global _TRK_INDEX
    if _TRK_INDEX is None:
        _TRK_INDEX = []
        for path in sorted(glob.glob(os.path.join(str(assets_dir), "*.TRK"))):
            try:
                _TRK_INDEX.append((os.path.basename(path), load_trk(open(path, "rb").read())))
            except Exception as e:
                _diag(f"load_trk {os.path.basename(path)}", e)
        if not _TRK_INDEX:
            print(f"[audio-obs] WARNING: no .TRK songs parsed from {assets_dir} -> "
                  "StartSong can never identify a song (no music).", flush=True)
    return _TRK_INDEX


def identify_song(mem, assets_dir) -> tuple[str, ModModule] | None:
    """Match the loaded in-memory module to its standard ``.TRK`` by order table.

    The song loader (0x02cc) copies the module's order list to ``[0xDC7]`` and its
    length to ``[0xDC2]``; the order sequence is a strong fingerprint of the song."""
    order = _a.read_order_table(mem)
    song_length = _a.read_song_length(mem)
    target = list(order[:song_length])
    if not target:
        return None
    idx = _trk_index(assets_dir)
    for name, mod in idx:                               # exact order match
        if list(mod.order) == target:
            return name, mod
    for name, mod in idx:                               # tolerant prefix match
        n = min(len(mod.order), len(target))
        if n >= 4 and list(mod.order[:n]) == target[:n]:
            return name, mod
    return None


def make_start_song(mem, assets_dir, *, loop: bool = True) -> StartSong | None:
    """Build a :class:`StartSong` for the song just loaded into VM memory, or ``None`` if
    no song is loaded.

    Always carries the **recovered** in-memory module (:func:`capture_module`) — the
    canonical song both audio systems branch from — so the rooted path never depends on
    ``.TRK`` identification. The standard ``.TRK`` (``module``/``name``) is attached when
    it can be matched, for the legacy clean-room player + diagnostics."""
    order = _a.read_order_table(mem)
    song_length = _a.read_song_length(mem)
    if not song_length or not any(order[:song_length + 1]):
        return None
    recovered = capture_module(mem)
    found = identify_song(mem, assets_dir)
    if found is None:
        return StartSong(module=None, recovered_module=recovered, name="", loop=loop)
    name, mod = found
    return StartSong(module=mod, recovered_module=recovered, name=name, loop=loop)


# --- live observers: emit events while the original game runs ----------------------

def install_command_observers(cpu, emit, assets_dir, *, also_run_original=None):
    """Install a transparent SFX hook + return a per-frame ``poll`` for song/music.

    ``emit`` is called with each :class:`~pre2.audio.events.GameAudioEvent`; ``assets_dir``
    is where the ``.TRK`` songs live (to root ``StartSong`` in the standard asset).

    * **play_sfx (0x0282)** is hooked at entry: ``dl`` and the descriptor table are both
      valid there, so each SFX command is caught exactly once. The hook runs the real
      instruction (``also_run_original``) so the original audio path is unchanged — a
      backend plays the event stream instead of the SB PCM.
    * **StartSong / music flag** are detected by the returned ``poll(mem=None)``, which the
      caller invokes once per frame: the song loader fills ``[0xDC2]``/``[0xDC7]`` over a
      full routine (not observable from a single entry instruction), so polling the order
      signature at a frame boundary is the reliable trigger. Fires once per real change.

    Returns ``poll`` (also called once now for the initial state)."""
    if also_run_original is None:
        from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook as also_run_original

    seen = {"order": None, "music": None}

    def on_play_sfx(c):
        try:
            if sfx_enabled(c.mem):
                emit(resolve_sfx(c.mem, c.s.dx & 0xFF))
        except Exception as e:
            _diag("play_sfx", e)
        also_run_original(c)

    cpu.replacement_hooks[(CODE_SEG, PLAY_SFX)] = on_play_sfx
    cpu.hook_names[(CODE_SEG, PLAY_SFX)] = "obs:play_sfx"

    def poll(mem=None):
        m = cpu.mem if mem is None else mem
        try:
            on = music_enabled(m)
            if on != seen["music"]:
                seen["music"] = on
                emit(SetMusicEnabled(on))
            sig = (bytes(_a.read_order_table(m)), _a.read_song_length(m))
            if sig != seen["order"]:
                seen["order"] = sig
                ev = make_start_song(m, assets_dir)
                if ev is not None:
                    seen["starts"] = seen.get("starts", 0) + 1
                    # The rooted path always has the recovered module; the .TRK name is just a
                    # label ("[recovered]" when we couldn't match a standard .TRK).
                    label = ev.name or "[recovered]"
                    print(f"[audio-obs] StartSong #{seen['starts']}: {label} "
                          f"(order_len={sig[1]}) -- should fire ONCE per real song change",
                          flush=True)
                    emit(ev)
        except Exception as e:
            _diag("poll", e)

    poll()
    return poll
