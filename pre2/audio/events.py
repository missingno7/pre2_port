"""Semantic game-audio events — the boundary contract.

The recovered command layer (:mod:`pre2.bridge.audio_commands`) emits these as the
game issues its original audio commands; the backends consume them. They describe
*what* the game wants to hear, not *how* the DOS mixer produced it — no segment
pointers, no fill buffers, no DMA/ISR/block sizes leak across this boundary.

Events are self-contained (they carry the resolved sample / module asset) so a
backend needs neither VM memory nor the asset files to play them.
"""
from __future__ import annotations

from dataclasses import dataclass

from pre2.audio.assets import SOURCE_RATE
from pre2.codecs.audio import ModModule

__all__ = [
    "GameAudioEvent", "PlaySfx", "StartSong", "StopSong",
    "SetMusicEnabled", "SetSfxEnabled", "SetVolume",
]


@dataclass(frozen=True)
class GameAudioEvent:
    """Base for all semantic audio events."""


@dataclass(frozen=True)
class PlaySfx(GameAudioEvent):
    """Play sound effect ``sfx_id`` once (recovered from play_sfx @ 1030:0282, dl=id).

    ``pcm`` is the resolved 8-bit unsigned effect sample; ``volume`` is on the
    original 0..0x40 scale. ``pan``/``priority`` are advisory (the enhanced backend
    may honour them; the faithful backend ignores them — the original had neither)."""
    sfx_id: int
    pcm: bytes
    volume: int = 0x40
    pan: float = 0.0
    priority: int = 0
    source_rate: int = SOURCE_RATE


@dataclass(frozen=True)
class StartSong(GameAudioEvent):
    """Start playing the song the loader (@ 1030:02cc) just installed.

    ``module`` is the matching standard ProTracker ``.TRK`` asset, identified from the loaded
    order table by :func:`pre2.bridge.audio_commands.identify_song` (``None`` when
    unidentified). The live enhanced player plays this whole module on its own clock; the
    recovery layer's only job here is to discover *which* song started. ``name`` is the source
    ``.TRK`` filename (diagnostics)."""
    module: ModModule | None = None
    name: str = ""
    song_id: int = 0
    loop: bool = True
    fade_ms: int = 0


@dataclass(frozen=True)
class StopSong(GameAudioEvent):
    """Stop the current song (optionally with an enhanced-only fade)."""
    fade_ms: int = 0


@dataclass(frozen=True)
class SetMusicEnabled(GameAudioEvent):
    """The music-enabled flag (cs:[3] bit 0x40) changed."""
    enabled: bool


@dataclass(frozen=True)
class SetSfxEnabled(GameAudioEvent):
    """The digital-SFX-enabled state changed."""
    enabled: bool


@dataclass(frozen=True)
class SetVolume(GameAudioEvent):
    """Master volume change (enhanced-only mixing aid); ``None`` leaves a bus alone."""
    music: float | None = None
    sfx: float | None = None
