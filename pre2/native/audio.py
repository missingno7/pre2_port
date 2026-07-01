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

from pre2.bridge.audio import read_sfx
from pre2.bridge.audio_commands import make_start_song, resolve_sfx, sfx_enabled, song_load_fingerprint

_DS = 0x1A0F << 4
_SONG_LENGTH = 0xDC2      # [asm 22FE] number of order positions
_ORDER_TABLE = 0xDC7      # [asm 22B3] order table (pattern sequence)


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
        self._sfx = None       # last SFX descriptor emitted (fire once per new sound)

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

        # --- SFX: the recovered play_sfx wrote a new [0x1004]/[0x1006] descriptor; emit PlaySfx once ---
        try:
            sfx = read_sfx(state)
        except Exception:                                       # noqa: BLE001 (no SFX state yet)
            return
        key = (sfx.pos, sfx.remaining)
        if sfx.remaining and key != self._sfx and sfx_enabled(state):
            self._sfx = key
            self._emit(resolve_sfx(state, _sfx_index(state)))
        elif not sfx.remaining:
            self._sfx = None


def _sfx_index(state) -> int:
    """The active SFX index the recovered play_sfx selected — the descriptor at [0x1004]/[0x1006] equals the
    table entry ``[0x1009 + dl*4]``, so recover ``dl`` by matching the live src against the descriptor table."""
    from pre2.bridge.audio_commands import SFX_TABLE
    d = state.data
    base = (0x1A0F << 4)
    src = d[base + 0x1004] | (d[base + 0x1005] << 8)
    for dl in range(0x40):
        t = base + SFX_TABLE + dl * 4
        if (d[t] | (d[t + 1] << 8)) == src:
            return dl
    return 0
