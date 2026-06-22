"""Live enhanced-audio runtime — an asynchronous wrapper around the deterministic core.

    game / VM / recovered command layer
        --post(command)-->  AudioCommandQueue (thread-safe)
            --> LiveEnhancedAudioEngine  (audio thread, owns the clock)
                --> RecoveredEnhancedBackend  (deterministic core)
                    --> RecoveredAudioSystem + EnhancedRenderer
                        --> AudioDevice  (continuous output)

The game thread ONLY enqueues semantic commands (``PlaySfx`` / ``StartSong`` / ``StopSong`` /
``SetMusicEnabled`` / ...). It never touches playback state and never renders. Only the audio
thread drains the queue and mutates the core, advancing by the **audio device's clock** (the
device's buffer back-pressure paces rendering). So music tempo is independent of game/VM speed,
renderer load, frame pacing, and of any Sound Blaster / DMA / IRQ timing.

The deterministic core (:class:`~pre2.audio.recovered_enhanced_backend.RecoveredEnhancedBackend`)
stays usable **synchronously** for tests / oracle comparison — this class only adds the queue,
the thread, and buffering on top. :meth:`render` (drain-then-render) is itself synchronous and
deterministic given a fixed command order, so the engine is testable without a real device.

Real-time discipline: the only work on the audio thread is draining the queue (cheap) and
rendering float blocks (numpy, ~90x real time). The heavy work — VM memory reads, ``capture_module``,
SQZ decode, ``.TRK`` parsing — happens on the *game* thread inside the command layer, before a
self-contained command is posted. The audio thread does no file/VM I/O and no logging.
"""
from __future__ import annotations

import queue
import threading
from typing import Protocol

import numpy as np

from pre2.audio.enhanced_render import OUT_RATE
from pre2.audio.events import GameAudioEvent
from pre2.audio.recovered_enhanced_backend import RecoveredEnhancedBackend

__all__ = ["LiveEnhancedAudioEngine", "AudioDevice"]


class AudioDevice(Protocol):
    """The minimal output sink the engine drives. Implementations own the actual device
    (e.g. an SDL/pygame channel) and provide the playback clock via their buffer state."""

    rate: int
    channels: int

    def busy(self) -> bool: ...          # is a chunk currently playing?
    def has_queue(self) -> bool: ...     # is a chunk already queued ahead of the current one?
    def play(self, pcm: bytes) -> None: ...   # start playing this chunk now
    def queue(self, pcm: bytes) -> None: ...  # queue this chunk after the current one
    def close(self) -> None: ...


class LiveEnhancedAudioEngine:
    """Owns the command queue, the deterministic core, the audio thread, and buffering."""

    def __init__(self, backend: RecoveredEnhancedBackend | None = None, *,
                 out_rate: int = OUT_RATE, free_run: bool = True,
                 status: dict | None = None) -> None:
        self.backend = backend if backend is not None else \
            RecoveredEnhancedBackend(out_rate=out_rate, free_run=free_run)
        self.status = status                     # optional HUD dict, refreshed by pump()
        self._queue: queue.Queue = queue.Queue()
        self._device: AudioDevice | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._chunk = 0
        self._started = False
        # diagnostics (read-only from the game thread)
        self.underruns = 0
        self.errors = 0
        self.commands_applied = 0

    # -- game thread: enqueue only -------------------------------------------------
    def post(self, command: GameAudioEvent) -> None:
        """Enqueue a semantic audio command. Thread-safe, returns immediately, never mutates
        playback state. (Drop-in for the command layer's ``emit`` callback.)"""
        self._queue.put(command)

    # -- deterministic, synchronous (audio thread AND tests/oracle) ----------------
    def _apply_commands(self) -> int:
        applied = 0
        while True:
            try:
                command = self._queue.get_nowait()
            except queue.Empty:
                break
            self.backend.handle(command)
            applied += 1
        self.commands_applied += applied
        return applied

    def render(self, n_frames: int) -> np.ndarray:
        """Drain any pending commands, then render ``n_frames`` of float32 stereo. Synchronous
        and deterministic for a fixed command order — the core of the testable contract."""
        self._apply_commands()
        return self.backend.render(n_frames)

    # -- live thread ---------------------------------------------------------------
    def start(self, device: AudioDevice, *, chunk_ms: float = 185.0) -> None:
        """Begin asynchronous playback on ``device``. The device's buffer state is the clock."""
        self._device = device
        self.backend.out_rate = device.rate
        self._chunk = max(256, int(device.rate * max(10.0, float(chunk_ms)) / 1000.0))
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="live-enhanced-audio", daemon=True)
        self._thread.start()

    def _chunk_bytes(self) -> bytes:
        stereo = self.render(self._chunk)                       # (chunk, 2) float32, [-1, 1]
        if self._device.channels == 1:
            stereo = stereo.mean(axis=1, keepdims=True)
        data = np.clip(stereo * 32767.0, -32768, 32767).astype(np.int16)
        return np.ascontiguousarray(data).tobytes()

    def _run(self) -> None:
        dev = self._device
        period = max(0.003, self._chunk / dev.rate / 3.0)       # poll a few times per chunk
        while not self._stop.is_set():
            try:
                if not dev.busy():
                    # Idle: first start, a real underrun (we fell behind), or just silence at a
                    # menu. Only a *playing* song that gaps is an audible glitch.
                    if self._started and self.backend.system.playing:
                        self.underruns += 1
                    self._started = True
                    dev.play(self._chunk_bytes())
                    dev.queue(self._chunk_bytes())
                elif not dev.has_queue():
                    dev.queue(self._chunk_bytes())
            except Exception:           # never let the audio thread die; no logging in the loop
                self.errors += 1
            self._stop.wait(period)

    # -- drop-in lifecycle for the front-end ---------------------------------------
    def pump(self) -> None:
        """Per-frame hook from the game loop. Does NO audio work (the audio thread owns
        timing); only refreshes the optional HUD status dict. Drop-in for the old feeder."""
        if self.status is not None:
            self.status.update(self.diagnostics())

    def diagnostics(self) -> dict:
        d = dict(self.backend.diagnostics())
        d.update(enh_underruns=str(self.underruns), enh_errors=str(self.errors),
                 enh_cmds=str(self.commands_applied))
        return d

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)
        if self._device is not None:
            self._device.close()
