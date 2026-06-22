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


class EnhancedAudio:
    """Play the enhanced backend's float32 stereo mix on a dedicated AUDIO thread.

    Fully detached from the DOS audio machine: SDL plays the queued PCM chunks on its own
    audio clock, and a background thread keeps the channel fed by pulling
    :meth:`EnhancedBackend.render`. The VM injects only **semantic events** via
    :meth:`handle` (``StartSong`` / ``PlaySfx`` / ``SetMusicEnabled``); the song free-runs
    at its own musical tempo. No SB blocks, DMA, IRQ timing, or original-mixer PCM are
    read here. A slow/jittery video frame cannot starve or gap the audio.

    A lock guards the backend (render on the audio thread vs. handle from the main thread).
    """

    def __init__(self, pygame, backend, sound_blaster=None, status: dict | None = None, *,
                 chunk_ms: float = 185.0) -> None:
        # chunk_ms drives the app-level buffer depth: pygame's Channel.queue only holds
        # ONE chunk ahead, so the headroom before an underrun is ~2*chunk_ms. The audio
        # thread shares the GIL with the (CPU-bound) VM thread, so small chunks starve and
        # crackle; ~120 ms (=> ~240 ms headroom) absorbs frame/VM jitter. The song clock is
        # the device, so a larger buffer adds latency but NEVER changes tempo. (A true fix
        # is a callback/ring-buffer stream that pulls in the audio driver's own thread,
        # independent of the GIL -- pygame has no callback API, so we buffer generously.)
        import threading
        self._pygame = pygame
        self._backend = backend
        self._sb = sound_blaster
        self._status = status
        self._available = False
        self._channel = None
        self._thread = None
        if not pygame.mixer.get_init():
            pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)
        init = pygame.mixer.get_init()
        if init is None:
            return
        self._rate = int(init[0])
        self._out_channels = int(init[2])
        backend.out_rate = self._rate                 # render at the device rate
        self._chunk = max(256, int(round(self._rate * max(10.0, float(chunk_ms)) / 1000.0)))
        if pygame.mixer.get_num_channels() < 3:
            pygame.mixer.set_num_channels(4)
        self._channel = pygame.mixer.Channel(2)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._available = True
        # Diagnostics: an underrun (the channel went idle because we didn't feed it in
        # time -> audible gap/crackle) is the prime suspect for "crackles", and a restart
        # is when we had to ch.play() a fresh chunk after one. Surfaced so a busy frame
        # starving this (GIL-shared) thread is visible, not silent.
        self._underruns = 0
        self._restarts = 0
        self._errors = 0
        self._started = False
        self._thread = threading.Thread(target=self._run, name="enhanced-audio", daemon=True)
        self._thread.start()

    def handle(self, event) -> None:
        """Inject a semantic audio event (called from the VM/main thread)."""
        with self._lock:
            self._backend.handle(event)

    def _make_chunk(self):
        with self._lock:
            stereo = self._backend.render(self._chunk)   # (chunk, 2) float32 in [-1, 1]
        block = stereo.mean(axis=1, keepdims=True) if self._out_channels == 1 else stereo
        data = np.clip(block * 32767, -32768, 32767).astype(np.int16)
        return self._pygame.mixer.Sound(buffer=np.ascontiguousarray(data).tobytes())

    def _is_playing(self) -> bool:
        """Whether a song is actually sounding (so an idle channel is a real gap, not just
        the silent title/menu where 'underruns' would be inaudible + alarmist)."""
        sysm = getattr(self._backend, "system", None)
        return bool(getattr(sysm, "playing", False)) if sysm is not None else True

    def _run(self) -> None:
        # Audio clock: SDL plays queued chunks continuously; we just keep one queued
        # ahead.  Checking a few times per chunk keeps the channel fed even if the main
        # (renderer) thread stalls -- audio never underruns because of a slow frame.
        period = max(0.003, self._chunk / self._rate / 3.0)
        while not self._stop.is_set():
            try:
                ch = self._channel
                if not ch.get_busy():
                    # Channel idle: first start, a real UNDERRUN (both the playing + queued
                    # chunk drained before we refilled -> an audible gap), or just silence at
                    # the title/menu. Only count it as a glitch when a song is actually playing.
                    if self._started and self._is_playing():
                        self._underruns += 1
                        if self._underruns <= 5 or self._underruns % 50 == 0:
                            print(f"[enhanced-audio] UNDERRUN #{self._underruns} "
                                  "(audio thread starved -> gap/crackle)", flush=True)
                    self._started = True
                    self._restarts += 1
                    ch.play(self._make_chunk())
                    ch.queue(self._make_chunk())
                elif ch.get_queue() is None:
                    ch.queue(self._make_chunk())
            except Exception as exc:
                self._errors += 1
                if self._errors <= 5:
                    import traceback
                    print(f"[enhanced-audio] thread error #{self._errors}: "
                          f"{type(exc).__name__}: {exc}", flush=True)
                    traceback.print_exc()
            if self._status is not None:
                self._status["enh_underruns"] = str(self._underruns)
                self._status["enh_errors"] = str(self._errors)
            self._stop.wait(period)

    def pump(self) -> None:
        # Housekeeping only: the enhanced mixer is fully detached from the DOS audio
        # machine -- it free-runs the song on the audio thread, driven solely by semantic
        # events (StartSong / PlaySfx / SetMusicEnabled).  We don't read or play the SB's
        # PCM at all; just drop it so the (unused) capture buffer doesn't grow.
        sb = self._sb
        if sb is not None and sb.pcm_out:
            sb.pcm_out.clear()
        # Surface the rooted backend's audio red-flags (StartSong repeats, missed SFX, native
        # tick cadence) on the HUD/log — the enhanced output never READS the SB above.
        if self._status is not None:
            diag = getattr(self._backend, "diagnostics", None)
            if diag is not None:
                self._status.update(diag())

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=0.5)
        if self._channel is not None:
            self._channel.stop()


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
    pal = np.array(palette if palette is not None else DEFAULT_VGA_PALETTE, dtype=np.uint8)
    start = display_start & 0xFFFF
    rowbase = (start + np.arange(HEIGHT) * _PLANAR_ROW_BYTES) & 0xFFFF
    off = (rowbase[:, None] + np.arange(_PLANAR_ROW_BYTES)[None, :]) & 0xFFFF   # (200,40)
    color = np.zeros((HEIGHT, _PLANAR_ROW_BYTES, 8), dtype=np.uint8)
    for plane in range(4):
        plane_bytes = arr[EGA_APERTURE + plane * EGA_PLANE_STRIDE + off]        # (200,40)
        bits = np.unpackbits(plane_bytes[..., None], axis=2)                    # (200,40,8) MSB-first
        color |= bits << plane
    return pal[color.reshape(HEIGHT, WIDTH)]
