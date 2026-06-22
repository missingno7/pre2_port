"""Rooted enhanced backend — the live event sink for the modern audio path.

The drop-in the live audio thread (``scripts/sdl_view.EnhancedAudio``) drives: it consumes
the recovered command :mod:`pre2.audio.events` stream into the single
:class:`~pre2.audio.recovered_system.RecoveredAudioSystem`, then renders it with the modern
:class:`~pre2.audio.enhanced_render.EnhancedRenderer`. This is what replaces the clean-room
``EnhancedBackend``/``ModPlayer`` destination: the live enhanced output is now rooted in the
same recovered model as the faithful path (``StartSong`` carries the recovered ``Module``),
not a parallel ``.TRK`` player.

Interface matches the audio thread's expectations: ``handle(event)`` (from the VM/main
thread), ``render(n_frames) -> (n, 2) float32`` (on the audio thread), and a settable
``out_rate`` (the device rate, applied once at start-up).
"""
from __future__ import annotations

import numpy as np

from pre2.audio.enhanced_render import OUT_RATE, EnhancedRenderer
from pre2.audio.events import GameAudioEvent, PlaySfx, StartSong
from pre2.audio.recovered_system import RecoveredAudioSystem

__all__ = ["RecoveredEnhancedBackend"]


class RecoveredEnhancedBackend:
    """Recovered command events -> RecoveredAudioSystem -> EnhancedRenderer (float32 stereo).

    ``free_run`` (default) ticks the recovered sequencer at the song's own tempo on the audio
    clock — a stable native clock with no Sound Blaster / DMA / IRQ involvement. The song
    model is the recovered :class:`Module` carried by ``StartSong`` (never the ``.TRK``)."""

    def __init__(self, out_rate: int = OUT_RATE, *, free_run: bool = True) -> None:
        self.system = RecoveredAudioSystem()
        self._free_run = free_run
        self._out_rate = out_rate
        self._renderer = EnhancedRenderer(self.system, out_rate=out_rate, free_run=free_run)
        # Diagnostics (the user's red-flag list): a song should StartSong once per real change;
        # a re-StartSong of the SAME order is suspicious (the bug behind restarts/"tempo" jumps).
        self.start_songs = 0
        self.repeated_start_songs = 0      # StartSong with an unchanged order signature
        self.unrooted_start_songs = 0      # StartSong missing a recovered module (no audio)
        self.sfx_played = 0
        self.sfx_missed = 0                # PlaySfx that carried no PCM
        self._last_order: tuple | None = None

    @property
    def out_rate(self) -> int:
        return self._out_rate

    @out_rate.setter
    def out_rate(self, rate: int) -> None:
        # The audio thread sets this once to the device rate before playback; rebuild the
        # renderer at that rate (the RecoveredAudioSystem — the model + clock — is preserved).
        self._out_rate = int(rate)
        self._renderer = EnhancedRenderer(self.system, out_rate=self._out_rate,
                                          free_run=self._free_run)

    # -- event sink (VM/main thread) ------------------------------------------
    def handle(self, event: GameAudioEvent) -> None:
        if isinstance(event, StartSong):
            if event.recovered_module is None:
                self.unrooted_start_songs += 1     # a song loaded but we couldn't capture it
                return
            order = tuple(event.recovered_module.order[:event.recovered_module.song_length + 1])
            if order == self._last_order:
                self.repeated_start_songs += 1
            self._last_order = order
            self.start_songs += 1
            self.system.start_song(event.recovered_module)
        elif isinstance(event, PlaySfx):
            if event.pcm:
                self.sfx_played += 1
            else:
                self.sfx_missed += 1
            self.system.handle(event)
        else:
            self.system.handle(event)

    def advance_ticks(self, k: int) -> None:
        """Supply game-audio ticks (ignored in free-run; for a game-paced clock variant)."""
        self._renderer.advance_ticks(k)

    def diagnostics(self) -> dict[str, str]:
        """Surface the audio red-flags (for the viewer HUD / logs)."""
        return {
            "enh_songs": str(self.start_songs),
            "enh_song_repeat": str(self.repeated_start_songs),
            "enh_song_unrooted": str(self.unrooted_start_songs),
            "enh_sfx": str(self.sfx_played),
            "enh_sfx_missed": str(self.sfx_missed),
            "enh_tick_hz": f"{self._renderer.tick_cadence_hz():.1f}",
        }

    # -- output (audio thread) ------------------------------------------------
    def render(self, n_frames: int) -> np.ndarray:
        return self._renderer.render(n_frames)
