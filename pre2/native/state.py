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
from pre2.views.memory_adapter import DATA_SEG   # the game data segment (DGROUP)


class NativeGameState:
    """The recovered game's memory image. Exposes ``.data`` (the 1 MB address space) so the existing bridges
    — which take a ``mem``-like object and index ``mem.data`` — read and write it with no change."""

    __slots__ = ("data", "backend", "sfx_queue", "particle_capture", "flash_slots", "song_request",
                 "boss_glyph", "snow_plots", "particle_capture_last", "flash_slots_last", "_hud_frozen_predeath",
                 "rng", "player")

    def __init__(self, data: bytearray):
        if not isinstance(data, bytearray):
            data = bytearray(data)
        if len(data) < ADDR_SPACE:
            data = data + bytearray(ADDR_SPACE - len(data))
        self.data = data
        #: THE swappable DGROUP memory backend (the field-backed flip's seam). Defaults to a ByteBackend over
        #: ``.data`` — a byte-exact no-op — so every rb/rw/wb/ww and the write-contract applier go through one
        #: object. Phase 4 swaps this for a hybrid (named state in a FieldBackend, only the residue in bytes);
        #: ``.data`` stays as the materialised image the renderer reads. See docs/pre2/offset_quarantine_plan.md.
        from pre2.views.dgroup_view import ByteBackend
        self.backend = ByteBackend(self)
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
        self._hud_frozen_predeath = None    # last HUD state drawn before a death (frozen through the death-bounce)
        #: the shipped, offset-free RNG object — the native-dataclass lift's (docs/pre2/native_dataclass_lift.md)
        #: first live P2 cluster. Seeded here from whatever ``.data`` currently holds (via the existing,
        #: offset-based RngView(self) — no bridge import, the offset stays on the descriptor through the P2/P3
        #: transition) so every construction path (cold boot / snapshot load / demo seed) re-seeds it correctly.
        #: Callers that construct an RngView register it against THIS instance so every roll lands on the same
        #: object; nothing here makes RNG live by itself.
        from pre2.game.model import Rng
        from pre2.views.dgroup_view import RngView, ScrollScriptView
        rv = RngView(self)
        self.rng = Rng(lcg_a=rv.lcg_a, lcg_b=rv.lcg_b, lcg_c=rv.lcg_c, lcg_d=rv.lcg_d, ror=rv.ror)
        self.backend.register(RngView, self.rng)   # any RngView(state) call now resolves live, no bridge needed
        # ScrollScriptView aliases the SAME 4 bytes under different names (rng_a../lcg_a..) for the LEVELG
        # snow/scroll subsystem (pre2/recovered/scroll_script.py) — remap so it rolls the SAME Rng object
        # instead of a byte image that would silently fork from RngView's copy.
        self.backend.register(ScrollScriptView, self.rng,
                              remap={"rng_a": "lcg_a", "rng_b": "lcg_b", "rng_c": "lcg_c", "rng_d": "lcg_d"})
        #: the shipped, offset-free PLAYER object — the native-dataclass lift's second live P2 cluster. Seeded
        #: from whatever ``.data`` currently holds (via the existing, offset-based ``PlayerView(self)`` — no
        #: bridge import). Deliberately NOT registered on ``self.backend`` globally (unlike ``self.rng``):
        #: doing so once made EVERY direct ``PlayerView(state)`` construction anywhere in the codebase resolve
        #: live -- including one-shot event paths (level_state.py's game-over/respawn reset, cave-teleport,
        #: attract-title, cold-boot) that write a player field and expect it in ``.data`` IMMEDIATELY, with no
        #: sync point of their own. That silently broke several (caught by the full test suite: a game-over
        #: reset's death-pose write vanished into an object nothing later flushed). Instead ``self.player`` is
        #: a TRANSACTIONAL handle: ``active_player()`` re-seeds it from CURRENT ``.data`` on every call, and a
        #: caller that threads it into one or more passes MUST call ``sync_player_to_image()`` immediately
        #: after (before anything else reads a player byte, and before calling ``active_player()`` again --
        #: re-fetching before syncing would silently discard the unflushed writes). See native/player.py's
        #: native_player_step and native/loop.py's native_gameplay_frame for the two call sites that follow
        #: this discipline.
        from pre2.game.model import Player
        from pre2.views.dgroup_view import PlayerView
        pv = PlayerView(self)
        self.player = Player(x=pv.x, y=pv.y, sprite=pv.sprite, xvel=pv.xvel, motion_mode=pv.motion_mode,
                             facing=pv.facing, anim_b=pv.anim_b, anim_ptr=pv.anim_ptr, yvel=pv.yvel,
                             run_flag=pv.run_flag, death_state=pv.death_state)

    @classmethod
    def from_vm(cls, rt) -> "NativeGameState":
        """Seed from a loaded VM runtime (a snapshot's memory) — the bootstrap into native state ownership."""
        return cls(bytearray(rt.cpu.mem.data))

    def active_rng(self):
        """The tick's authoritative live RNG, or ``None`` when nothing should be registered (let the caller's
        overlay fall through to the pure offset path instead):

        - the DEFAULT ``ByteBackend`` (the common case, and the only backend ``self.rng`` was ever seeded
          from/kept in sync with) -> ``self.rng``;
        - a backend that carries its own object-graph copy (duck-typed ``.rng``, e.g. the object-graph
          ``DataclassBackend`` — matching the 6913 second-pass dual-path in pre2/native/loop.py, no bridge
          import here) -> that copy;
        - anything else (e.g. the field-backed ``HybridBackend``, which has no ``.rng`` at all and already
          tracks RNG correctly through its own offset-keyed named-field store) -> ``None``. Falling back to
          ``self.rng`` here would silently fork a THIRD, stale copy from whatever that backend actually
          tracks — ``self.rng`` is only ever updated while ``self.backend`` is the original ``ByteBackend``."""
        from pre2.views.dgroup_view import ByteBackend
        be = self.backend
        if isinstance(be, ByteBackend):
            return self.rng
        return getattr(be, "rng", None)

    def sync_rng_to_image(self) -> None:
        """Fold ``self.rng`` back into the raw DGROUP image through the SAME offset-based ``RngView``
        descriptors (no bridge, no duplicated offset literal) — for anything that still needs fresh raw bytes
        directly (a snapshot dump, a digest compare). A fresh, unregistered ``ByteBackend`` bypasses the
        live-object registry so the write lands straight on ``.data``."""
        from pre2.views.dgroup_view import ByteBackend, RngView
        raw = RngView(ByteBackend(self))
        rng = self.rng
        raw.lcg_a, raw.lcg_b, raw.lcg_c, raw.lcg_d, raw.ror = rng.lcg_a, rng.lcg_b, rng.lcg_c, rng.lcg_d, rng.ror

    def active_player(self):
        """The tick's authoritative live Player, freshly RE-SEEDED from ``.data`` on every call, or ``None``
        when nothing should be used (let the caller's overlay fall through to the pure offset path instead).

        Re-seeding (not just returning ``self.player`` as-is) matters because ``self.player`` is NOT kept in
        sync automatically — nothing is registered on ``self.backend`` (see ``__init__``'s comment). A caller
        that fetches this once and threads the SAME returned object into one or more passes accumulates their
        writes correctly (they share the object, no re-seed in between). But calling this AGAIN before
        ``sync_player_to_image()`` flushes those writes back to ``.data`` would silently DISCARD them (the
        re-seed reads stale-relative-to-the-object-but-not-yet-updated ``.data``) — so every call site must
        fetch once, thread that one reference through its whole player-touching pass, then sync immediately.

        UNLIKE :meth:`active_rng`, this does NOT fall back to a non-``ByteBackend``'s own ``.player`` copy.
        RNG's fields are all plain ints on every backend, so reusing an object-graph backend's own ``Rng`` is
        harmless. Player's ``anim_ptr`` is NOT: the bridge (``pre2/bridge/game_layout.py``) swizzles it into an
        ``AssetCursor``/``RawRef`` for ``DataclassBackend``/``ObjectGraphStore``, and this registry's
        ``_registry_write_field``/``_read_field`` do ``v & mask`` assuming a plain int -- registering that
        swizzled object onto another overlay (e.g. tick_terrain_entities' ``ov``) crashes the moment any
        recovered function reads/writes ``p.anim_ptr`` (``TypeError: unsupported operand type(s) for &:
        'RawRef' and 'int'``). So: only the DEFAULT ``ByteBackend`` (guaranteed a plain int) returns a live
        object; anything else (the object-graph backend, which already tracks its OWN Player correctly through
        its own offset map with no FieldRegistry involved, or ``HybridBackend``, which tracks it via its own
        field store) -> ``None``."""
        from pre2.views.dgroup_view import ByteBackend, PlayerView
        if not isinstance(self.backend, ByteBackend):
            return None
        pv, p = PlayerView(self), self.player
        p.x, p.y, p.sprite, p.xvel = pv.x, pv.y, pv.sprite, pv.xvel
        p.motion_mode, p.facing, p.anim_b = pv.motion_mode, pv.facing, pv.anim_b
        p.anim_ptr, p.yvel, p.run_flag, p.death_state = pv.anim_ptr, pv.yvel, pv.run_flag, pv.death_state
        return p

    def sync_player_to_image(self) -> None:
        """Fold ``self.player`` back into the raw DGROUP image through the SAME offset-based ``PlayerView``
        descriptors (no bridge, no duplicated offset literal) -- via a fresh, unregistered ``PlayerView(self)``
        (``self.backend`` is never registered, so this is just the ordinary write path). MANDATORY immediately
        after every pass fetched via :meth:`active_player`, before anything else reads a player byte or calls
        ``active_player()`` again (see that method's docstring for why)."""
        from pre2.views.dgroup_view import PlayerView
        raw = PlayerView(self)
        p = self.player
        raw.x, raw.y, raw.sprite = p.x, p.y, p.sprite
        raw.xvel, raw.motion_mode, raw.facing = p.xvel, p.motion_mode, p.facing
        raw.anim_b, raw.anim_ptr, raw.yvel = p.anim_b, p.anim_ptr, p.yvel
        raw.run_flag, raw.death_state = p.run_flag, p.death_state

    def rb(self, off: int) -> int:
        """Read a DGROUP byte (DS-relative), the recovered functions' ``rb`` accessor — via the backend seam."""
        return self.backend.rb(off)

    def rw(self, off: int) -> int:
        """Read a DGROUP word (DS-relative), the recovered functions' ``rw`` accessor — via the backend seam."""
        return self.backend.rw(off)

    def wb(self, off: int, v: int) -> None:
        """Write a DGROUP byte (DS-relative) — via the backend seam."""
        self.backend.wb(off, v)

    def ww(self, off: int, v: int) -> None:
        """Write a DGROUP word (DS-relative) — via the backend seam."""
        self.backend.ww(off, v)
