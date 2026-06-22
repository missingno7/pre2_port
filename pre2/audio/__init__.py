"""Prehistorik 2 audio — the two-layer audio architecture.

The game's audio is exposed as a stream of **semantic** :mod:`~pre2.audio.events`
(``PlaySfx`` / ``StartSong`` / ``StopSong`` / ...), recovered from the original
audio command routines by :mod:`pre2.bridge.audio_commands`. Two interchangeable
backends consume that same stream:

* :class:`pre2.audio.faithful_backend.FaithfulBackend` — reproduces the original
  byte-exact 8-bit / ~8.4 kHz output via the recovered tracker + mixer (the
  archaeological oracle, kept for verification);
* :class:`pre2.audio.enhanced_backend.EnhancedBackend` — a modern float32 / 44.1 kHz
  mixer (HQ resampling, no 8-bit wrap, no DMA/ISR/168-byte-block constraints),
  using the original samples/modules only as **assets**.

The pivot: the game says *what* to play (semantic events); it no longer dictates
*how* the DOS mixer mixes it. See ``docs/pre2/audio_architecture.md``.
"""
