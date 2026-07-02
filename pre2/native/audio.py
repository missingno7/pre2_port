"""Native audio bridge — drive the enhanced (.TRK music + SFX) player from a NativeGameState, VM-free.

The recovered gameplay/front-end writes the audio-COMMAND state into the DGROUP byte-exact, exactly as the VM
does: the loaded song's order table ([0xDC7] / length [0xDC2]) and the active SFX descriptor ([0x1004] src /
[0x1006] remaining / [0x0B59] seg). The ``--audio enhanced`` player is COMMAND-DRIVEN — it plays the identified
standard ``.TRK`` module + the SFX PCM, NOT the VM's software mixer (no SB / DMA / IRQ / 8237 / 8259). So a
per-frame poll of the native state emits the SAME ``StartSong`` / ``PlaySfx`` events the VM's
``install_command_observers`` (pre2/bridge/audio_commands.py) emits from its CPU hooks — with no VM in the loop.

Wire ``emit`` to ``SdlEnhancedAudio.post`` (the object ``play.py --audio enhanced`` builds): the standalone
``play_native.py`` runner constructs the player + a ``NativeAudio`` and calls ``poll(state)`` once per frame.
The song poll mirrors the VM observers' ``poll`` (the loader fills the order over a routine, so a frame-boundary
order-signature change is the reliable trigger); the SFX poll fires on a new descriptor (what the recovered
play_sfx writes), the VM-less equivalent of the 0x0282 entry hook.
"""
from __future__ import annotations

from pre2.bridge.audio_commands import make_start_song, resolve_sfx, sfx_enabled, song_load_fingerprint

_DS = 0x1A0F << 4
_CS = 0x1030 << 4
_SONG_LENGTH = 0xDC2      # [asm 22FE] number of order positions
_ORDER_TABLE = 0xDC7      # [asm 22B3] order table (pattern sequence)
_SFX_DEV_FLAGS = (0x1D6C, 0x1D6D)   # cs: digital-device-present flags (either == 1 -> the SB/digital path)


def native_play_sfx(state, dl: int) -> None:
    """[asm 0282] Emit sound effect ``dl`` the way ``play_sfx`` does — reproduce its device dispatch:

    * a digital device is present (``cs:[0x1D6C]`` or ``cs:[0x1D6D]`` == 1) -> write the SB descriptor
      ``[0x1004]`` (src) / ``[0x1006]`` (len) from the table entry ``[0x1009 + dl*4]`` (0x2A9). This is exactly
      what :class:`NativeAudio`'s poll reads to fire a ``PlaySfx``.
    * otherwise -> write the PC-speaker active-note pointer ``[0x1035] = 0x1037 + dl*0xA`` (0x292).

    The standalone runner sets a device flag at boot (SB emulation is present), so it takes the digital path and
    plays; the forward oracle seeds the VM's flags (PC-speaker in the recorded demos), so it matches the VM's
    ``[0x1035]`` write instead. Byte-exact with 0282 in both."""
    d = state.data
    dl &= 0xFF
    if d[_CS + _SFX_DEV_FLAGS[0]] == 1 or d[_CS + _SFX_DEV_FLAGS[1]] == 1:   # [asm 0282/028a] digital present
        bx = dl * 4
        d[_DS + 0x1004] = d[_DS + 0x1009 + bx]; d[_DS + 0x1005] = d[_DS + 0x100A + bx]   # [asm 02b7-02bb] src
        d[_DS + 0x1006] = d[_DS + 0x100B + bx]; d[_DS + 0x1007] = d[_DS + 0x100C + bx]   # [asm 02be-02c2] len
    else:                                                                   # [asm 0292-02a1] PC-speaker note ptr
        ptr = (0x1037 + dl * 0xA) & 0xFFFF
        d[_DS + 0x1035] = ptr & 0xFF; d[_DS + 0x1036] = (ptr >> 8) & 0xFF
    q = getattr(state, "sfx_queue", None)                                   # every CALL is a distinct trigger (a
    if q is not None:                                                       #   repeated identical effect re-fires)
        q.append(dl)
        if len(q) > 64:                                                     # cap for a consumer-less run (the oracle)
            del q[:-64]


def native_emit_sfx(state, sfx) -> None:
    """Emit each ``play_sfx`` index a recovered pass produced (its ``sfx`` list), in order (last wins the single
    ``[0x1004]`` descriptor, as in the VM's per-frame sequence). A no-op for an empty list."""
    for dl in sfx:
        native_play_sfx(state, dl)


_SFX_BANK_SEG = 0xC000    # native home for the SFX PCM bank (upper memory, above the 0xA000 video aperture)


def native_load_sfx_bank(state, game_root: str, *, seg: int = _SFX_BANK_SEG) -> None:
    """[asm 07C9] Reproduce the sound-init's SFX sample-bank load so the VM-less runtime can PLAY effects.

    Decodes ``SAMPLE.SQZ`` (the 11 concatenated 8-bit-PCM @ 8000 Hz effects, 0xED60 bytes), places it at ``seg``,
    points the sample segment ``[0x0B59]`` there, fills each effect's source offset in the ``[0x1009 + dl*4]``
    ``{src,len}`` table (``src`` = prefix-sum of the lengths already loaded there), and flags a digital device
    present (``cs:[0x1D6C]``) so ``native_play_sfx`` takes the digital path (writes ``[0x1004]``/``[0x1006]``) and
    :class:`NativeAudio` fires ``PlaySfx``. Idempotent; call once at boot (the VM does it after OLDIES). The
    forward oracle seeds the VM's own audio state, so this only affects the standalone product runtime."""
    import os

    from pre2.codecs.sqz import unpack_sqz
    with open(os.path.join(game_root, "SAMPLE.SQZ"), "rb") as f:
        pcm = unpack_sqz(f.read())
    d = state.data
    base = (seg << 4) & 0xFFFFF
    d[base:base + len(pcm)] = pcm                            # the decoded sample bank
    d[_DS + 0x0B59] = seg & 0xFF; d[_DS + 0x0B5A] = (seg >> 8) & 0xFF        # [0x0B59] = sample segment
    src = 0
    for dl in range(11):                                    # fill each effect's src = running offset into the bank
        t = _DS + 0x1009 + dl * 4
        length = d[t + 2] | (d[t + 3] << 8)                # the len is already in the table (static)
        d[t] = src & 0xFF; d[t + 1] = (src >> 8) & 0xFF
        src = (src + length) & 0xFFFF
    d[_CS + _SFX_DEV_FLAGS[0]] = 1                          # digital device present -> the SB/digital path


# [asm 01AB-01B7] the per-level song: `bl=[0x2d8a]; al=[bx+0x2d20]; call 0x2cc` — the level indexes the song-index
# table [0x2D20], and the loader (0x2cc/0x492) maps that index to the in-EMS module. Native streams the .TRK from
# disk, so we map the song index -> filename here. The [0x2D20] table is read LIVE from the game state (it's static
# DGROUP data, always present); the index->file map was recovered by matching each level's loaded order table
# ([0xDC7], strict full match) to the disk .TRK across the snapshot corpus, plus the code-confirmed front-end songs.
_SONG_INDEX_TO_FILE = {
    0: "PRES.TRK",       # L3-L5 (distinctive 13-entry order; the identity-order matches are transient loads)
    1: "CARTE.TRK",      # the carte scroll-in [asm 96E3 ax=1]
    2: "CODE.TRK",       # the mode-select menu [asm 952D ax=2]
    4: "GLACE.TRK",      # L7/L8 (verified across the level-6/7 snapshots)
    9: "MINES.TRK",      # L1/L2 (verified across the level-0/1 snapshots + the menu->L1 demo)
    10: "MYSTERY.TRK",   # L9
    13: "MONSTER.TRK",   # L6/L10 (the boss levels)
    15: "BRAVO.TRK",     # the level-end tally [asm 4CE7 ax=0xf]
}


def native_level_song_name(state) -> str:
    """[asm 01AB-01B7] The music for the CURRENT level: song index = ``[0x2D20 + [0x2d8a]]`` mapped to its .TRK.
    The VM loads this right after the carte (main 01B7), which native's flow must reproduce so the level music
    replaces the carte song. Falls back to MINES for an unmapped index (any level still gets level music)."""
    d = state.data
    level = d[_DS + 0x2D8A]
    idx = d[_DS + 0x2D20 + level]
    return _SONG_INDEX_TO_FILE.get(idx, "MINES.TRK")


def native_load_song(state, name: str, game_root: str) -> None:
    """Reproduce the song loader (``1030:02cc``) for the VM-less runtime: parse the standard ``.TRK`` module's
    order list into ``[0xDC7]`` and its length into ``[0xDC2]`` — the fingerprint :class:`NativeAudio` matches to
    identify + play the song. (The enhanced player streams the ``.TRK`` from disk, so only the order/length state
    is needed; no module PCM has to be placed in memory.) Call it at the scene the VM loads the song: PRESENTA at
    the PRESENT title, CODE at the menu, CARTE at the carte, the level song at level start."""
    import os

    from pre2.codecs.audio import load_trk
    with open(os.path.join(game_root, name), "rb") as f:
        mod = load_trk(f.read())
    d = state.data
    order = mod.order
    d[_DS + _SONG_LENGTH] = len(order) & 0xFF
    for i, o in enumerate(order[:0x100]):
        d[_DS + _ORDER_TABLE + i] = o & 0xFF


class NativeAudio:
    """Per-frame audio-command poller over a NativeGameState.

    ``emit(command)`` receives each :class:`~pre2.audio.events.GameAudioEvent` (StartSong / PlaySfx) — pass
    ``SdlEnhancedAudio.post``. ``game_root`` is where the ``.TRK`` songs live (to root StartSong in the asset)."""

    def __init__(self, emit, game_root: str):
        self._emit = emit
        self._game_root = game_root
        self._song = None     # last song order-signature emitted (fire once per change)

    def poll(self, state) -> None:
        """Emit the audio commands the native frame just produced. Call once per displayed frame."""
        # --- music: the recovered loader wrote the order table; emit StartSong once it changes ---
        fp = song_load_fingerprint(state)                       # uses state.data (NativeGameState)
        if fp != self._song:
            self._song = fp
            if fp is not None:
                cmd = make_start_song(state, self._game_root)
                if cmd is not None:
                    self._emit(cmd)

        # --- SFX: native_play_sfx queued each TRIGGER this frame (one entry per call, so a repeated identical
        #     effect — a held attack hitting each frame — fires each time). Emit + drain. ---
        queue = getattr(state, "sfx_queue", None)
        if queue:
            if sfx_enabled(state):
                for dl in queue:
                    try:
                        self._emit(resolve_sfx(state, dl))
                    except Exception:                           # noqa: BLE001 (bank not loaded yet)
                        break
            queue.clear()
