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
from pre2.audio.events import GameAudioEvent, StartSong
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
        self.unrooted_start_songs = 0      # diagnostic: StartSong missing a recovered module

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
            self.system.start_song(event.recovered_module)
        else:
            self.system.handle(event)

    def advance_ticks(self, k: int) -> None:
        """Supply game-audio ticks (ignored in free-run; for a game-paced clock variant)."""
        self._renderer.advance_ticks(k)

    # -- output (audio thread) ------------------------------------------------
    def render(self, n_frames: int) -> np.ndarray:
        return self._renderer.render(n_frames)
