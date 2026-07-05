"""``NativeGameState`` — the recovered game's memory, owned without a VM.

It *is* the game's address space (a 1 MB ``bytearray`` exposed as ``.data``, exactly what the emulated VM's
``mem`` exposes), so every recovered function and every ``pre2/bridge`` adapter that already reads/writes VM
memory runs over it unchanged. That is the migration's adapter swap: the recovered function is the shared
centre; today a VM ``mem`` is one adapter, a ``NativeGameState`` is another — one implementation, two adapters,
no second copy that can drift.

Seeded today from a snapshot/VM (the bootstrap). As islands move to source-level state ownership, more of the
per-frame update reads/writes this image natively and the VM is needed only as the verify oracle.
"""
from __future__ import annotations

ADDR_SPACE = 0x100000   # 1 MB real-mode address space (DGROUP @ DS<<4, tilemap @ [0x2DDA], framebuffer, ...)
DATA_SEG = 0x1A0F       # the game data segment (DGROUP)


class NativeGameState:
    """The recovered game's memory image. Exposes ``.data`` (the 1 MB address space) so the existing bridges
    — which take a ``mem``-like object and index ``mem.data`` — read and write it with no change."""

    __slots__ = ("data", "sfx_queue", "particle_capture", "flash_slots", "song_request", "boss_glyph",
                 "snow_plots", "particle_capture_last", "flash_slots_last")

    def __init__(self, data: bytearray):
        if not isinstance(data, bytearray):
            data = bytearray(data)
        if len(data) < ADDR_SPACE:
            data = data + bytearray(ADDR_SPACE - len(data))
        self.data = data
        #: play_sfx TRIGGERS this frame (a list of effect indices). native_play_sfx appends one per CALL — so a
        #: repeated identical effect (e.g. a held attack hitting each frame) fires each time, unlike the single
        #: [0x1004] descriptor which is last-wins. NativeAudio drains it once per displayed frame; capped so a
        #: consumer-less run (the forward oracle) can't grow it without bound.
        self.sfx_queue: list[int] = []
        #: the [0x7DE6] point particles (spider-threads/sparkles/fireflies) snapshotted at the 4B8E ENTRY — i.e.
        #: BEFORE native_particle_consume kills them (they're one-shot). native_render draws from this so the
        #: effects show; None when unset (native_render then reads the live [0x7DE6], empty after the consume).
        self.particle_capture = None
        #: render-record slots whose OPAQUE/flash flag (id bit14 = [+5]&0x40) was set when the 26FA record-mutation
        #: ran this frame — the VM's 26FA reads it to draw the sprite as a solid white flash silhouette, then CLEARS
        #: it (asm 28BA) in the SAME pass, so it is gone by the commit boundary native_render reads. Captured by
        #: native_object_render_state (pre-clear) and re-applied by native_render so the hit/death flash shows.
        self.flash_slots: list[int] | None = None
        #: a song-load request from a recovered routine (the 7585 boss-music 02CC call, SONG_REQUEST sentinel):
        #: the 02CC song INDEX. NativeAudio.poll consumes it (loads the .TRK once per song change) — the gameplay
        #: frame itself stays file-free.
        self.song_request: int | None = None
        #: the mode-9 final-boss glyph id the script interpreter selected this tick ([asm 6BD3] -> the 6C0D
        #: render). The renderer (bridge _boss_glyph_tiles) reads it to draw the boss image; None off level 9.
        self.boss_glyph: int | None = None
        #: the LEVELG falling-snow pixels for this frame — a list of (page-relative byte offset, bit mask) the
        #: 3922 render half (native_scroll_script -> scroll_script_snow) plots white into the frame. Empty/None on
        #: every other level (wind [0x6bf6] == 0). native_render's effect pass (draw_snow) overlays them.
        self.snow_plots: list | None = None
        #: NON-DESTRUCTIVE stashes of the two one-shots above, left by native_render for SAME-tick consumers
        #: that run after it (the interpolation extractor in play_native). Overwritten on every render (None
        #: when the tick had none), so they can never go stale across ticks.
        self.particle_capture_last = None
        self.flash_slots_last: list[int] | None = None

    @classmethod
    def from_vm(cls, rt) -> "NativeGameState":
        """Seed from a loaded VM runtime (a snapshot's memory) — the bootstrap into native state ownership."""
        return cls(bytearray(rt.cpu.mem.data))

    def rb(self, off: int) -> int:
        """Read a DGROUP byte (DS-relative), the recovered functions' ``rb`` accessor."""
        return self.data[((DATA_SEG << 4) + (off & 0xFFFF)) & 0xFFFFF]

    def rw(self, off: int) -> int:
        """Read a DGROUP word (DS-relative), the recovered functions' ``rw`` accessor."""
        b = ((DATA_SEG << 4) + (off & 0xFFFF)) & 0xFFFFF
        return self.data[b] | (self.data[(b + 1) & 0xFFFFF] << 8)
