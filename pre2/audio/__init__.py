"""Prehistorik 2 audio — semantic-command architecture.

The game's audio is exposed as a stream of **semantic** :mod:`~pre2.audio.events`
(``StartSong`` / ``PlaySfx`` / ``StopSong`` / ``SetMusicEnabled``), recovered from the
original audio command routines by :mod:`pre2.bridge.audio_commands`. The recovery layer's
job is to discover the high-level *intent* — which song starts (matched to its standard
``.TRK``), which SFX fire — not to dictate how it is mixed.

Two independent consumers of that intent:

* **Faithful** (the oracle): :class:`pre2.audio.faithful_backend.FaithfulBackend` reproduces
  the original byte-exact 8-bit / ~8.4 kHz output via the recovered tracker + mixer
  (:mod:`pre2.recovered`). Kept for verification (``pre2/probes/verify_audio_system.py``).

* **Enhanced** (the live, modern path): ``scripts/sdl_view.SdlEnhancedAudio`` hands the whole
  identified ``.TRK`` module to SDL_mixer's MOD player, which streams it on SDL's own audio
  clock — so music tempo is owned by the audio device, never by VM / render / frame timing.
  It does not touch the recovered tracker/mixer, the Sound Blaster, or original PCM.

This package holds the neutral asset/event model (:mod:`~pre2.audio.assets`,
:mod:`~pre2.audio.events`) and the faithful backend; the live SDL player lives in the viewer
(``scripts/sdl_view.py``). See ``docs/pre2/audio_architecture.md``.
"""
