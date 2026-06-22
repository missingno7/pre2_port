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

from pre2.bridge import audio as _a
from pre2.audio.assets import SOURCE_RATE, Module, SampleAsset
from pre2.audio.events import PlaySfx, SetMusicEnabled, StartSong, StopSong

CODE_SEG = _a.CODE_SEG
DATA_SEG = _a.DATA_SEG

PLAY_SFX = 0x0282          # entry: dl = effect index
SONG_LOADER = 0x02CC       # entry: loads the M.K. module
SFX_TABLE = 0x1009         # DS:0x1009 + dl*4 -> {src word, len word}
SFX_DEV_FLAGS = (0x1D6C, 0x1D6D)   # cs: digital-device-present flags (either == 1)

__all__ = [
    "resolve_sfx", "capture_module", "capture_start_song", "sfx_enabled", "music_enabled",
    "install_command_observers",
]


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


def capture_start_song(mem, *, song_id: int = 0, loop: bool = True) -> StartSong:
    return StartSong(module=capture_module(mem), song_id=song_id, loop=loop)


# --- live observers: emit events while the original game runs ----------------------

def install_command_observers(cpu, emit, *, also_run_original=None) -> None:
    """Install transparent hooks that emit semantic events as the game issues commands.

    ``emit`` is called with each :class:`~pre2.audio.events.GameAudioEvent`. The hooks
    are *observers*: they read state then run the real instruction (via
    ``also_run_original``, normally ``interpret_current_instruction_without_hook``),
    so the original audio path is unchanged — a backend can play the event stream
    alongside (or instead of) the SB output. StartSong is detected by the module
    signature changing, so it fires once per actual song load."""
    if also_run_original is None:
        from dos_re.bootstrap_lzexe import interpret_current_instruction_without_hook as also_run_original

    seen = {"order": None, "music": None}

    def on_play_sfx(c):
        try:
            if sfx_enabled(c.mem):
                dl = c.s.dx & 0xFF
                emit(resolve_sfx(c.mem, dl))
        except Exception:
            pass
        also_run_original(c)

    def on_song_loader(c):
        also_run_original(c)   # let the loader fill the module first, then observe it
        try:
            order = _a.read_order_table(c.mem)
            sig = (order, _a.read_song_length(c.mem))
            if sig != seen["order"]:
                seen["order"] = sig
                emit(capture_start_song(c.mem))
        except Exception:
            pass

    def maybe_music_flag(c):
        on = music_enabled(c.mem)
        if on != seen["music"]:
            seen["music"] = on
            emit(SetMusicEnabled(on))

    cpu.replacement_hooks[(CODE_SEG, PLAY_SFX)] = on_play_sfx
    cpu.hook_names[(CODE_SEG, PLAY_SFX)] = "obs:play_sfx"
    cpu.replacement_hooks[(CODE_SEG, SONG_LOADER)] = on_song_loader
    cpu.hook_names[(CODE_SEG, SONG_LOADER)] = "obs:song_loader"
    # music-flag changes are observed lazily off the SFX/song hooks + first song load
    maybe_music_flag(cpu)
