"""VM audio-command observation — the WORKBENCH side of the audio seam.

Installs a transparent hook on the game's play_sfx entry (0x0282) + a per-frame poll for song/music changes,
emitting semantic :class:`~pre2.audio.events.GameAudioEvent`\ s for the workbench viewer (scripts/play.py).
The PRODUCT never runs this — pre2.views.audio_commands holds the pure command DECODERS (resolve_sfx,
make_start_song, ...) both sides share; this module owns the dos_re hook plumbing (detachable)."""
from __future__ import annotations

from pre2.audio.events import SetMusicEnabled
from pre2.views import audio as _a  # noqa: F401 — the shared command-window constants
from pre2.views.audio_commands import (CODE_SEG, PLAY_SFX, _diag, identify_song, make_start_song,
                                       music_enabled, resolve_sfx, sfx_enabled, song_load_fingerprint)


def install_command_observers(cpu, emit, assets_dir, *, also_run_original=None):
    """Install a transparent SFX hook + return a per-frame ``poll`` for song/music.

    ``emit`` is called with each :class:`~pre2.audio.events.GameAudioEvent`; ``assets_dir``
    is where the ``.TRK`` songs live (to root ``StartSong`` in the standard asset).

    * **play_sfx (0x0282)** is hooked at entry: ``dl`` and the descriptor table are both
      valid there, so each SFX command is caught exactly once. The hook runs the real
      instruction (``also_run_original``) so the game's own command code still executes and
      its state stays consistent; the player plays the emitted semantic event.
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
                # Wait for the loader to FINISH: capture only when the song's static data has
                # stopped changing (this frame's fingerprint == last frame's). Capturing mid-
                # load snapshots a half-built song that plays silent. The recovered audio ISR
                # is stubbed, so once loaded the fingerprint stays constant.
                fp = song_load_fingerprint(m)
                stable = fp is not None and fp == seen.get("load_fp")
                seen["load_fp"] = fp
                ev = make_start_song(m, assets_dir) if stable else None
                if ev is not None:
                    seen["order"] = sig
                    seen["starts"] = seen.get("starts", 0) + 1
                    # ev.name is the identified .TRK; "" when no .TRK matched the order (the
                    # player can't play it and reports it as unrooted).
                    label = ev.name or "[unidentified]"
                    print(f"[audio-obs] StartSong #{seen['starts']}: {label} "
                          f"(order_len={sig[1]}) -- should fire ONCE per real song change",
                          flush=True)
                    emit(ev)
        except Exception as e:
            _diag("poll", e)

    poll()
    return poll
