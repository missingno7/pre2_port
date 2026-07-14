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

from pre2.views.audio_commands import make_start_song, resolve_sfx, sfx_enabled, song_load_fingerprint
from pre2.views.dgroup_view import AudioGlobals, PlayerGlobals, PlayerView
from pre2.views.tables import Tables
from pre2.native.state import DATA_SEG

_CS = 0x1030 << 4      # code segment — the SFX device-flag reads (native_play_sfx) are CS-relative, not DGROUP
_DS = DATA_SEG << 4
_ORDER_TABLE = 0xDC7      # [asm 22B3] order table (pattern sequence)
_SFX_DEV_FLAGS = (0x1D6C, 0x1D6D)   # cs: digital-device-present flags (either == 1 -> the SB/digital path)


def sfx_screen_x(state, world_x: int) -> int:
    """The on-screen X (0..320-ish) of a world-space X for the stereo pan: ``world_x - camera``. The camera
    pixel X is ``[0x2DE4]`` (tiles) * 16. May land <0 or >320 (off-screen); the poll clamps the pan."""
    cam_px = PlayerGlobals(state).cam_col_word * 16
    return world_x - cam_px


def player_sfx_x(state) -> int:
    """The player's on-screen X — the pan for the player's own sounds (jump, throw, hurt, death scream, glider)."""
    return sfx_screen_x(state, PlayerView(state).x)


def native_play_sfx(state, dl: int, x: "int | None" = None) -> None:
    """[asm 0282] Emit sound effect ``dl`` the way ``play_sfx`` does — reproduce its device dispatch:

    ``x`` is the OPTIONAL trigger screen X (0..320) for the enhanced STEREO pan (None = centre); it only rides
    the native ``sfx_queue`` side-channel, never the byte-exact ``play_sfx`` writes below.

    * a digital device is present (``cs:[0x1D6C]`` or ``cs:[0x1D6D]`` == 1) -> write the SB descriptor
      ``[0x1004]`` (src) / ``[0x1006]`` (len) from the table entry ``[0x1009 + dl*4]`` (0x2A9). This is exactly
      what :class:`NativeAudio`'s poll reads to fire a ``PlaySfx``.
    * otherwise -> write the PC-speaker active-note pointer ``[0x1035] = 0x1037 + dl*0xA`` (0x292).

    The standalone runner sets a device flag at boot (SB emulation is present), so it takes the digital path and
    plays; the forward oracle seeds the VM's flags (PC-speaker in the recorded demos), so it matches the VM's
    ``[0x1035]`` write instead. Byte-exact with 0282 in both."""
    d = state.data
    dl &= 0xFF
    g = AudioGlobals(state)
    if d[_CS + _SFX_DEV_FLAGS[0]] == 1 or d[_CS + _SFX_DEV_FLAGS[1]] == 1:   # [asm 0282/028a] digital present
        entry = g.sfx_entries[dl]                                            # the SB {src,len} descriptor table
        g.sfx_src = entry.src                                                # [asm 02b7-02bb]
        g.sfx_len = entry.length                                             # [asm 02be-02c2]
    else:                                                                   # [asm 0292-02a1] PC-speaker note ptr
        ptr = (0x1037 + dl * 0xA) & 0xFFFF
        g.sfx_pcspk_note = ptr
    q = getattr(state, "sfx_queue", None)                                   # every CALL is a distinct trigger (a
    if q is not None:                                                       #   repeated identical effect re-fires)
        q.append((dl, x))                                                   # x = trigger SCREEN X (0..320) or None
        if len(q) > 64:                                                     # cap for a consumer-less run (the oracle)
            del q[:-64]


def native_emit_sfx(state, sfx, x: "int | None" = None) -> None:
    """Emit each ``play_sfx`` index a recovered pass produced (its ``sfx`` list), in order (last wins the single
    ``[0x1004]`` descriptor, as in the VM's per-frame sequence). A no-op for an empty list. ``x`` is the optional
    trigger screen X for the enhanced stereo pan (applies to every index in this batch)."""
    for dl in sfx:
        native_play_sfx(state, dl, x)


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
    d[base:base + len(pcm)] = pcm                            # the decoded sample bank (segment 0xC000, not DS)
    g = AudioGlobals(state)
    g.sfx_sample_seg = seg                                           # [0x0B59] = sample segment
    src = 0
    for dl in range(11):                                    # fill each effect's src = running offset into the bank
        entry = g.sfx_entries[dl]                           # the {src,len} descriptor table entry
        length = entry.length                                # the len is already in the table (static)
        entry.src = src
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
    3: "PRESENTA.TRK",   # the title/MENU song [asm 0107/015a ax=3] (loaded by name in native_menu_flow too)
    4: "GLACE.TRK",      # L7/L8 (verified across the level-6/7 snapshots)
    9: "MINES.TRK",      # L1/L2 (verified across the level-0/1 snapshots + the menu->L1 demo)
    10: "MYSTERY.TRK",   # L9
    13: "MONSTER.TRK",   # L6/L10 (the boss levels)
    14: "FINAL.TRK",     # L0xE = the final "© 1993 TITUS" room (fingerprint-matched vs the L0xE snapshot's order)
    15: "BRAVO.TRK",     # the level-end tally [asm 4CE7 ax=0xf]
}


def native_level_song_name(state) -> str:
    """[asm 01AB-01B7] The music for the CURRENT level: song index = ``[0x2D20 + [0x2d8a]]`` mapped to its .TRK.
    The VM loads this right after the carte (main 01B7), which native's flow must reproduce so the level music
    replaces the carte song. Falls back to MINES for an unmapped index (any level still gets level music)."""
    level = PlayerGlobals(state).level
    idx = Tables(state.rb).song_index[level]                    # [0x2D20+level] the per-level song-index table
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
    order = mod.order
    AudioGlobals(state).song_length = len(order) & 0xFF
    order_bytes = bytes(o & 0xFF for o in order[:0x100])
    base = _DS + _ORDER_TABLE
    state.data[base:base + len(order_bytes)] = order_bytes


class NativeAudio:
    """Per-frame audio-command poller over a NativeGameState.

    ``emit(command)`` receives each :class:`~pre2.audio.events.GameAudioEvent` (StartSong / PlaySfx) — pass
    ``SdlEnhancedAudio.post``. ``game_root`` is where the ``.TRK`` songs live (to root StartSong in the asset)."""

    def __init__(self, emit, game_root: str):
        self._emit = emit
        self._game_root = game_root
        self._song = None     # last song order-signature emitted (fire once per change)
        self._req_file = None  # last song_request .TRK loaded (gate the per-frame boss re-request)
        self.stereo = False    # ENHANCED: pan SFX by their on-screen trigger X (else all centre / faithful)

    def poll(self, state) -> None:
        """Emit the audio commands the native frame just produced. Call once per displayed frame."""
        # --- a recovered routine requested a song by 02CC INDEX (the 7585 boss-music seam): load its .TRK
        #     order table once per change (the request repeats every boss frame, exactly like the ASM re-calling
        #     02CC; the fingerprint below then emits StartSong once) ---
        req = getattr(state, "song_request", None)
        loaded_by_req = False
        if req is not None:
            state.song_request = None
            name = _SONG_INDEX_TO_FILE.get(req)
            if name is not None and name != self._req_file:     # the per-frame boss-request de-dup (load once)
                self._req_file = name
                native_load_song(state, name, self._game_root)
                loaded_by_req = True
        # --- music: the recovered loader wrote the order table; emit StartSong once it changes ---
        fp = song_load_fingerprint(state)                       # uses state.data (NativeGameState)
        if fp != self._song:
            self._song = fp
            if not loaded_by_req:
                # An EXTERNAL song load (native_load_song at a level start / tally / carte) overwrote the order
                # table — the "already loaded this request" latch (_req_file) is now STALE. Clear it, else a
                # LATER boss re-request of the SAME index (e.g. MONSTER again after an intervening level song)
                # is wrongly suppressed and the boss music never switches back (verified: boss2 stayed on the
                # level song). Not cleared for our own req load (that would re-load the .TRK every boss frame).
                self._req_file = None
            if fp is not None:
                cmd = make_start_song(state, self._game_root)
                if cmd is not None:
                    self._emit(cmd)

        # --- SFX: native_play_sfx queued each TRIGGER this frame (one entry per call, so a repeated identical
        #     effect — a held attack hitting each frame — fires each time). Emit + drain. ---
        queue = getattr(state, "sfx_queue", None)
        if queue:
            if sfx_enabled(state):
                for dl, x in queue:
                    pan = 0.0
                    if self.stereo and x is not None:           # screen X 0..320 -> pan -1..+1 (centre 160)
                        pan = max(-1.0, min(1.0, (x - 160.0) / 160.0))
                    try:
                        self._emit(resolve_sfx(state, dl, pan=pan))
                    except Exception:                           # noqa: BLE001 (bank not loaded yet)
                        break
            queue.clear()
