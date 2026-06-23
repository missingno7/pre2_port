"""SDL/pygame display + audio backend for the interactive DOS_RE viewer (``play.py``).

PRE2 uses BIOS text, VGA DAC palettes, and EGA/VGA-compatible planar graphics
(mode 0Dh) for the game screens; early bring-up snapshots may also use linear
VGA mode 13h.  This module provides those decoders plus the Nuked-OPL3 audio backend.  Frames
are decoded with vectorised NumPy at native 320x200 (independent of ``--scale``)
and uploaded straight to an SDL surface, which keeps the present round-trip cheap
enough to stay interactive.

The graphics decoders are pixel-identical to the reference PPM decoders in
``render_frame.py`` (asserted by ``tests/test_render_rgb.py``); that renderer
remains the headless PNG-dump tool and decode oracle, while this module is what
the live viewer uses.

``play.py`` imports this module only when it actually launches the viewer, so the
core runtime, the PNG tool and the tests do not require ``pygame``.
"""
from __future__ import annotations

import os

import numpy as np

from render_frame import DEFAULT_VGA_PALETTE
from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE

WIDTH, HEIGHT = 320, 200
_PLANAR_ROW_BYTES = 40   # 320 px / 8 px-per-byte, EGA/VGA 16-colour planar (mode 0Dh)

_TEXT_MODES = {0, 1, 2, 3, 7}
_TEXT_PALETTE = [
    (0x00, 0x00, 0x00), (0x00, 0x00, 0xAA), (0x00, 0xAA, 0x00), (0x00, 0xAA, 0xAA),
    (0xAA, 0x00, 0x00), (0xAA, 0x00, 0xAA), (0xAA, 0x55, 0x00), (0xAA, 0xAA, 0xAA),
    (0x55, 0x55, 0x55), (0x55, 0x55, 0xFF), (0x55, 0xFF, 0x55), (0x55, 0xFF, 0xFF),
    (0xFF, 0x55, 0x55), (0xFF, 0x55, 0xFF), (0xFF, 0xFF, 0x55), (0xFF, 0xFF, 0xFF),
]

_DOS_5X7_PATTERNS: dict[str, tuple[str, ...]] = {
    " ": ("00000", "00000", "00000", "00000", "00000", "00000", "00000"),
    "!": ("00100", "00100", "00100", "00100", "00100", "00000", "00100"),
    '"': ("01010", "01010", "01010", "00000", "00000", "00000", "00000"),
    "#": ("01010", "11111", "01010", "01010", "11111", "01010", "01010"),
    "$": ("00100", "01111", "10100", "01110", "00101", "11110", "00100"),
    "%": ("11001", "11010", "00100", "01000", "01011", "10011", "00000"),
    "&": ("01100", "10010", "10100", "01000", "10101", "10010", "01101"),
    "'": ("00100", "00100", "01000", "00000", "00000", "00000", "00000"),
    "(": ("00010", "00100", "01000", "01000", "01000", "00100", "00010"),
    ")": ("01000", "00100", "00010", "00010", "00010", "00100", "01000"),
    "*": ("00000", "10101", "01110", "11111", "01110", "10101", "00000"),
    "+": ("00000", "00100", "00100", "11111", "00100", "00100", "00000"),
    ",": ("00000", "00000", "00000", "00000", "00110", "00100", "01000"),
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    ".": ("00000", "00000", "00000", "00000", "00000", "01100", "01100"),
    "/": ("00001", "00010", "00100", "01000", "10000", "00000", "00000"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("00110", "01000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00010", "11100"),
    ":": ("00000", "01100", "01100", "00000", "01100", "01100", "00000"),
    ";": ("00000", "01100", "01100", "00000", "01100", "00100", "01000"),
    "<": ("00010", "00100", "01000", "10000", "01000", "00100", "00010"),
    "=": ("00000", "00000", "11111", "00000", "11111", "00000", "00000"),
    ">": ("01000", "00100", "00010", "00001", "00010", "00100", "01000"),
    "?": ("01110", "10001", "00001", "00010", "00100", "00000", "00100"),
    "@": ("01110", "10001", "10111", "10101", "10111", "10000", "01111"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01111"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("01110", "00100", "00100", "00100", "00100", "00100", "01110"),
    "J": ("00001", "00001", "00001", "00001", "10001", "10001", "01110"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
    "[": ("01110", "01000", "01000", "01000", "01000", "01000", "01110"),
    "\\": ("10000", "01000", "00100", "00010", "00001", "00000", "00000"),
    "]": ("01110", "00010", "00010", "00010", "00010", "00010", "01110"),
    "^": ("00100", "01010", "10001", "00000", "00000", "00000", "00000"),
    "_": ("00000", "00000", "00000", "00000", "00000", "00000", "11111"),
    "`": ("01000", "00100", "00010", "00000", "00000", "00000", "00000"),
    "|": ("00100", "00100", "00100", "00100", "00100", "00100", "00100"),
    "~": ("00000", "00000", "01000", "10101", "00010", "00000", "00000"),
}

_TEXT_GLYPH_CACHE: dict[int, np.ndarray] = {}


def _bitmap_mask_for_code(ch: int) -> np.ndarray:
    """Return a crisp 8x16 bitmap mask for a BIOS text character.

    BIOS text-mode screens are character-cell devices, not graphics surfaces.
    Rendering them through pygame's proportional outline font makes them look
    like scaled UI text.  A small ROM-like 5x7 bitmap expanded into an 8x16 cell
    keeps text deterministic, monospace, and nearest-neighbour friendly.  CP437
    box/extended glyphs fall back to '?' until we need them.
    """
    ch &= 0xFF
    cached = _TEXT_GLYPH_CACHE.get(ch)
    if cached is not None:
        return cached
    if 0x61 <= ch <= 0x7A:
        key = chr(ch - 0x20)
    elif 0x20 <= ch <= 0x7E:
        key = chr(ch)
    else:
        key = "?"
    rows = _DOS_5X7_PATTERNS.get(key, _DOS_5X7_PATTERNS["?"])
    mask = np.zeros((16, 8), dtype=bool)
    y0 = 1
    x0 = 1
    for src_y, row_bits in enumerate(rows):
        for src_x, bit in enumerate(row_bits):
            if bit == "1":
                mask[y0 + src_y * 2:y0 + src_y * 2 + 2, x0 + src_x] = True
    _TEXT_GLYPH_CACHE[ch] = mask
    return mask


def render_text_rgb(mem: bytes, mode: int, page: int = 0) -> np.ndarray:
    """Decode BIOS 80x25 text memory to a native 640x400 RGB image.

    This intentionally does not use host fonts.  The source screen is already a
    character-cell bitmap device, so using a deterministic bitmap mask avoids
    anti-aliased/proportional glyph artifacts and makes integer SDL scaling look
    much closer to a DOS text screen.
    """
    base = 0xB0000 if (mode & 0xFF) == 7 else 0xB8000
    page_off = (page & 0x07) * 0x1000
    cell_w, cell_h = 8, 16
    arr = np.zeros((25 * cell_h, 80 * cell_w, 3), dtype=np.uint8)
    mem_arr = np.frombuffer(mem, dtype=np.uint8)
    for row in range(25):
        y = row * cell_h
        for col in range(80):
            x = col * cell_w
            off = base + page_off + ((row * 80 + col) * 2)
            if off + 1 >= mem_arr.size:
                continue
            ch = int(mem_arr[off]) or 0x20
            attr = int(mem_arr[off + 1])
            fg = _TEXT_PALETTE[attr & 0x0F]
            bg = _TEXT_PALETTE[(attr >> 4) & 0x07]
            cell = arr[y:y + cell_h, x:x + cell_w]
            cell[:, :] = bg
            mask = _bitmap_mask_for_code(ch)
            cell[mask] = fg
    return arr


class SoundBlasterAudio:
    """Play the PCM the emulated Sound Blaster streams over DMA.

    The VM runs the original SB driver, which DMA's 8-bit unsigned PCM (the game's
    software MOD+SFX mix) at the sample rate it programmed.  This drains that
    captured stream, resamples it to the mixer rate, and plays it on its own mixer
    channel so it mixes with the OPL (AdLib) output.
    """

    def __init__(self, pygame, sound_blaster, status: dict | None = None, *,
                 chunk_ms: float = 46.0) -> None:
        self._pygame = pygame
        self._sb = sound_blaster
        self._status = status
        self._available = False
        self._channel = None
        self._rate = 44100
        self._channels = 1
        self._buf = np.zeros(0, dtype=np.int16)
        self._in = np.zeros(0, dtype=np.float64)  # carried resampler input tail
        self._phase = 0.0                         # carried fractional read position
        if not pygame.mixer.get_init():
            pygame.mixer.init(frequency=44100, size=-16, channels=1, buffer=512)
        init = pygame.mixer.get_init()
        if init is None:
            return
        self._rate = int(init[0])
        self._channels = int(init[2])
        self._chunk = max(256, int(round(self._rate * max(10.0, float(chunk_ms)) / 1000.0)))
        # The game produces PCM in per-frame bursts (~22/s); play through a jitter
        # buffer so playback never drains between bursts.  Don't start (or restart
        # after an underrun) until this much audio is queued ahead.
        self._lead = int(self._rate * 0.12)   # ~120 ms
        self._started = False
        if pygame.mixer.get_num_channels() < 3:
            pygame.mixer.set_num_channels(4)
        self._channel = pygame.mixer.Channel(2)
        self._available = True

    def pump(self) -> None:
        if not self._available or self._channel is None:
            return
        self._drain()
        if not self._started:
            # Build the lead before (re)starting; avoids click-restart loops.
            if len(self._buf) >= self._lead:
                self._channel.play(self._next_chunk())
                if len(self._buf) >= self._chunk:
                    self._channel.queue(self._next_chunk())
                self._started = True
            return
        if not self._channel.get_busy():
            self._started = False        # underran -> rebuild the lead, don't click-spam
            return
        # Keep one chunk queued ahead, but only ever emit *full* chunks (no silence pad).
        if self._channel.get_queue() is None and len(self._buf) >= self._chunk:
            self._channel.queue(self._next_chunk())

    def _drain(self) -> None:
        sb = self._sb
        if not sb.pcm_out:
            return
        raw = bytes(sb.pcm_out)
        sb.pcm_out.clear()
        src_rate = sb.sample_rate or 8000
        # SB 8-bit DMA is *unsigned*; map it to signed and play it straight through, just
        # like the real card's DAC -- this matches DOSBox (incl. the game's own low DC
        # rest level near byte 0x40 and its asymmetric upward peaks, which are how the game
        # actually sounds).  We deliberately do NOT remove the DC offset: it is inaudible
        # (real hardware output is AC-coupled), and subtracting it re-centres the very
        # asymmetric waveform so its positive peaks overshoot +full-scale and hard-clip.
        # `(byte-128)*256` always fits int16 by construction, so played as-is it never clips.
        sig = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128) * 256
        # Phase-continuous LINEAR resample to the mixer rate.  Carrying the fractional
        # read position (`_phase`) and the unconsumed input tail (`_in`) across drains
        # avoids the per-block phase reset and sample-and-hold steps of a
        # nearest-neighbour resample, which otherwise click at every ~20 ms DMA-block
        # boundary and sound gritty.  (Upsample => ratio < 1, so we always keep >=1
        # input sample of tail for the next interpolation.)
        self._in = np.concatenate([self._in, sig])
        ratio = src_rate / self._rate                      # input samples per output
        avail = len(self._in)
        if avail >= 2:
            k = int((avail - 1 - self._phase) / ratio) + 1
            pos = self._phase + np.arange(max(0, k)) * ratio
            pos = pos[pos <= avail - 1]
            if len(pos):
                i0 = np.floor(pos).astype(np.int64)
                frac = pos - i0
                i1 = np.minimum(i0 + 1, avail - 1)
                out = self._in[i0] * (1.0 - frac) + self._in[i1] * frac
                self._buf = np.concatenate(
                    [self._buf, np.clip(out, -32768, 32767).astype(np.int16)])
                adv = self._phase + len(pos) * ratio
                consumed = int(adv)
                self._in = self._in[consumed:]
                self._phase = adv - consumed
        cap = self._rate                                   # cap buffered latency at ~1s
        if len(self._buf) > cap:
            self._buf = self._buf[-cap:]
        if len(self._in) > cap:                            # safety: never let tail grow
            self._in = self._in[-cap:]

    def _next_chunk(self):
        # Emit up to one chunk of whatever is buffered (callers gate on having a
        # full chunk for the queue path; the initial play path may emit the lead).
        n = min(self._chunk, len(self._buf)) or 1
        chunk, self._buf = self._buf[:n], self._buf[n:]
        if self._channels > 1:
            chunk = np.repeat(chunk[:, None], self._channels, axis=1)
        return self._pygame.mixer.Sound(buffer=chunk.astype(np.int16).tobytes())

    def close(self) -> None:
        if self._channel is not None:
            self._channel.stop()


class SdlEnhancedAudio:
    """Modern enhanced audio: a command-driven player that owns a CONTINUOUS clock.

    The recovery/command layer discovers high-level *intent* — which song starts (the
    identified standard ``.TRK`` module), when it stops, which SFX fire — and hands whole
    semantic commands here. This player then plays the complete module with SDL_mixer's MOD
    player, which streams on SDL's own C audio thread. So music tempo is owned entirely by the
    audio device and can NOT be slowed by Python/VM/render/frame scheduling or queue
    starvation (the live clocking bug). It does not touch the recovered tracker/mixer, the SB,
    DMA blocks, or original PCM — those belong to the faithful oracle path.

    Commands (called from the game thread; SDL does the streaming):
    ``StartSong`` -> load + play the song's standard MOD; ``StopSong`` -> stop;
    ``SetMusicEnabled`` -> mute/unmute; ``PlaySfx`` -> one-shot on a mixer channel.
    """

    def __init__(self, pygame, assets_dir, status: dict | None = None, *,
                 music_volume: float = 0.65, sfx_volume: float = 0.7) -> None:
        self._pygame = pygame
        self._assets_dir = str(assets_dir)
        self._status = status
        self._music_volume = music_volume
        self._sfx_volume = sfx_volume
        if not pygame.mixer.get_init():
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)
        init = pygame.mixer.get_init() or (44100, -16, 2)
        self._rate = int(init[0])
        self._out_ch = int(init[2])
        pygame.mixer.set_num_channels(16)
        self._sfx_channels = [pygame.mixer.Channel(i) for i in range(8)]
        self._rr = 0
        self._mod_cache: dict[str, bytes] = {}
        self._cur_io = None                # keep a ref so SDL can stream from it
        self._music_on = True
        self._last_name = None
        # diagnostics (the user's red-flag list)
        self.start_songs = 0
        self.song_repeat = 0
        self.song_unrooted = 0             # StartSong with no identified .TRK (can't play)
        self.sfx_played = 0
        self.sfx_missed = 0
        self.errors = 0

    # -- the song's standard MOD bytes (decompressed once, cached; off the audio path) --
    def _mod_bytes(self, name: str) -> bytes:
        if name not in self._mod_cache:
            from pre2.codecs.audio import unpack_sqz
            with open(os.path.join(self._assets_dir, name), "rb") as f:
                self._mod_cache[name] = unpack_sqz(f.read())
        return self._mod_cache[name]

    def _sfx_sound(self, ev):
        """Resolved 8-bit signed SFX -> a pygame Sound at the mixer's 16-bit stereo format."""
        a = np.frombuffer(ev.pcm, dtype=np.int8).astype(np.float32) / 128.0
        src = ev.source_rate or 8000
        n_out = max(1, int(len(a) * self._rate / src))
        idx = np.clip((np.arange(n_out) * src / self._rate).astype(np.int64), 0, len(a) - 1)
        mono = np.clip(a[idx] * self._sfx_volume * 32767.0, -32768, 32767).astype(np.int16)
        data = mono if self._out_ch == 1 else np.repeat(mono[:, None], 2, axis=1)
        return self._pygame.mixer.Sound(buffer=np.ascontiguousarray(data).tobytes())

    # -- command sink (game thread) ----------------------------------------------------
    def post(self, command) -> None:
        """Drop-in for the command layer's ``emit``. SDL owns timing, so handling a command is
        just a quick control op (load/play/stop/volume) — safe from the game thread."""
        from pre2.audio.events import (
            PlaySfx, SetMusicEnabled, SetSfxEnabled, StartSong, StopSong,
        )
        try:
            if isinstance(command, StartSong):
                if not command.name:                  # only identified standard .TRK songs
                    self.song_unrooted += 1
                    return
                if command.name == self._last_name:
                    self.song_repeat += 1
                self._last_name = command.name
                self.start_songs += 1
                import io
                self._cur_io = io.BytesIO(self._mod_bytes(command.name))
                self._pygame.mixer.music.load(self._cur_io)
                self._pygame.mixer.music.set_volume(self._music_volume if self._music_on else 0.0)
                self._pygame.mixer.music.play(-1 if command.loop else 0)
            elif isinstance(command, StopSong):
                self._pygame.mixer.music.stop()
            elif isinstance(command, SetMusicEnabled):
                self._music_on = command.enabled
                self._pygame.mixer.music.set_volume(self._music_volume if self._music_on else 0.0)
            elif isinstance(command, PlaySfx):
                if not command.pcm:
                    self.sfx_missed += 1
                    return
                self.sfx_played += 1
                self._sfx_channels[self._rr % len(self._sfx_channels)].play(self._sfx_sound(command))
                self._rr += 1
        except Exception as exc:               # never let a bad command kill the game loop
            self.errors += 1
            if self.errors <= 3:
                print(f"[enh-audio] command error: {type(exc).__name__}: {exc}", flush=True)

    def pump(self) -> None:
        """Per-frame hook (game thread). No audio work — SDL streams on its own clock; only
        refresh the HUD diagnostics. enh_tick_hz / enh_underruns are N/A: SDL owns the clock so
        there is no Python render cadence to drift and no queue to underrun."""
        if self._status is not None:
            self._status.update(
                enh_songs=str(self.start_songs), enh_song_repeat=str(self.song_repeat),
                enh_song_unrooted=str(self.song_unrooted), enh_sfx=str(self.sfx_played),
                enh_sfx_missed=str(self.sfx_missed), enh_errors=str(self.errors),
                enh_clock="SDL", enh_underruns="0(SDL)",
                text=(f"enh SDL-clock | songs={self.start_songs} repeat={self.song_repeat} "
                      f"unrooted={self.song_unrooted} | sfx={self.sfx_played} miss={self.sfx_missed} "
                      f"err={self.errors} | underruns=0(SDL) tick=SDL"))

    def close(self) -> None:
        try:
            self._pygame.mixer.music.stop()
        except Exception:
            pass


def render_vga_rgb(mem: bytes, palette: list[tuple[int, int, int]] | None = None) -> np.ndarray:
    """Decode VGA mode 13h A000:0000 linear 320x200x8bpp to RGB."""
    arr = np.frombuffer(mem, dtype=np.uint8)
    pal = np.array(palette if palette is not None else DEFAULT_VGA_PALETTE, dtype=np.uint8)
    idx = arr[0xA0000:0xA0000 + WIDTH * HEIGHT].reshape(HEIGHT, WIDTH)
    return pal[idx]


def render_planar_rgb(mem: bytes, display_start: int = 0,
                      palette: list[tuple[int, int, int]] | None = None) -> np.ndarray:
    """Decode a 320x200 16-colour planar screen (mode 0Dh) to RGB.

    The VM stores the four bit-planes in its shadow aperture at ``EGA_APERTURE``.
    Each byte is eight horizontal pixels (MSB first); the 4-bit colour index is
    one bit from each plane and is looked up through the live DAC ``palette`` (the
    attribute controller is identity for PRE2's screens).  This is what the viewer
    uses so the mode-0Dh intro/menu screens are visible while the VM still takes
    that 16-colour path instead of true VGA mode 13h.
    """
    arr = np.frombuffer(mem, dtype=np.uint8)
    start = display_start & 0xFFFF
    # Page-wrap for the menu/title screens. PRE2's mode-select present (1030:9600 / the pan at
    # ~97BE) masks the CRTC start with `and bh,0x1f` — it treats the display as a 0x2000-byte
    # CIRCULAR page: the scrolling background (and a wrapped glyph at the page edge) must wrap at
    # 0x2000, not read on into the next page. Detect such a single-page screen (start in page 0,
    # no plane content beyond 0x2000) and wrap the scanline read there. Gameplay fills the plane
    # with its scroll ring (content well past 0x2000) so it keeps the full 0x10000 wrap, unchanged.
    plane0 = arr[EGA_APERTURE:EGA_APERTURE + EGA_PLANE_STRIDE]
    wrap = 0x1FFF if (start < 0x2000 and not plane0[0x2000:].any()) else 0xFFFF
    return _planar_to_rgb(lambda p: arr[EGA_APERTURE + p * EGA_PLANE_STRIDE:
                                        EGA_APERTURE + (p + 1) * EGA_PLANE_STRIDE],
                          display_start, palette, wrap)


def render_planar_rgb_from_planes(planes, display_start: int = 0,
                                  palette: list[tuple[int, int, int]] | None = None) -> np.ndarray:
    """Decode four explicit 64 KiB EGA plane buffers (the recovered ``render_frame`` output) to RGB.

    The live-FAITHFUL viewer path: instead of reading the ASM-populated shadow aperture, this
    deplanarizes the planes the recovered renderer produced from a clean framebuffer. Gameplay fills
    the plane past 0x2000 so the full 0x10000 scanline wrap applies (no menu single-page wrap)."""
    parr = [np.frombuffer(bytes(p), dtype=np.uint8) for p in planes]
    return _planar_to_rgb(lambda p: parr[p], display_start, palette, 0xFFFF)


def _planar_to_rgb(get_plane, display_start: int, palette, wrap: int) -> np.ndarray:
    """Shared core: assemble the 4-bit colour index from four bit-planes (MSB-first) through the
    DAC. ``get_plane(p)`` returns plane p as a uint8 array indexable up to ``wrap``."""
    pal = np.array(palette if palette is not None else DEFAULT_VGA_PALETTE, dtype=np.uint8)
    start = display_start & 0xFFFF
    rowbase = (start + np.arange(HEIGHT) * _PLANAR_ROW_BYTES) & wrap
    off = (rowbase[:, None] + np.arange(_PLANAR_ROW_BYTES)[None, :]) & wrap     # (200,40)
    color = np.zeros((HEIGHT, _PLANAR_ROW_BYTES, 8), dtype=np.uint8)
    for plane in range(4):
        plane_bytes = get_plane(plane)[off]                                    # (200,40)
        bits = np.unpackbits(plane_bytes[..., None], axis=2)                    # (200,40,8) MSB-first
        color |= bits << plane
    return pal[color.reshape(HEIGHT, WIDTH)]
