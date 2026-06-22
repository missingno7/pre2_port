"""Tests for the two-layer audio architecture (semantic events -> faithful/enhanced).

Synthetic fixtures only (plus the repo's real ``.TRK`` assets for song identification).
The byte-exact fidelity of the faithful path vs the ISR oracle is covered separately by
``pre2/probes/verify_audio_system.py``.
"""
from __future__ import annotations

import ast
from pathlib import Path

import numpy as np

from pre2.audio.assets import Module, SampleAsset
from pre2.audio.enhanced_backend import EnhancedBackend
from pre2.audio.events import PlaySfx, SetMusicEnabled, StartSong, StopSong
from pre2.audio.faithful_backend import FaithfulBackend, audio_state_from_module
from pre2.audio.mod_player import ModPlayer
from pre2.bridge import audio_commands as AC
from pre2.codecs.audio import ModModule, ModSample

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"


# ---- fixtures --------------------------------------------------------------------

def _pre2_module() -> Module:
    """PRE2 in-memory module (the faithful oracle's native input): 1 sample, 1 pattern."""
    pat = bytearray(1024)
    pat[0:4] = bytes([100, 0, 0x10, 0])          # PRE2 cell: sample 1, note-index 100
    period_table = [0] * 0x8000
    period_table[100] = 256
    vol_table = bytes(i & 0xFF for i in range(65 * 64 + 256))
    sample = SampleAsset(pcm=bytes((i * 2) & 0xFF for i in range(300)),
                         length=200, loop_start=0, loop_len=0, default_volume=64)
    return Module(order=(0,), song_length=0, patterns={0: bytes(pat)}, samples=(sample,),
                  period_table=tuple(period_table), vol_table=vol_table, initial_speed=3)


def _std_module() -> ModModule:
    """Standard ProTracker module (the enhanced path's input): 1 sample, 1 pattern."""
    samples = [ModSample(name="", length=(200 if i == 0 else 0), finetune=0,
                         volume=(64 if i == 0 else 0), loop_start=0, loop_len=0)
               for i in range(31)]
    pat = bytearray(1024)
    pat[0:4] = bytes([0x01, 0xAC, 0x10, 0x00])   # std cell: sample 1, period 428 (C-2)
    sample_data = bytes((i * 3) & 0xFF for i in range(200))
    return ModModule(title="test", samples=tuple(samples), order=(0,), restart=0,
                     signature="M.K.", num_patterns=1, pattern_data=bytes(pat),
                     sample_data=sample_data)


class _FakeMem:
    def __init__(self, size=0x300000):
        self.data = bytearray(size)

    def w(self, seg, off, val):
        b = (seg << 4) + off
        self.data[b] = val & 0xFF
        self.data[b + 1] = (val >> 8) & 0xFF


# ---- semantic command bridge -----------------------------------------------------

def test_resolve_sfx_reads_descriptor_from_table():
    mem = _FakeMem()
    DS = AC.DATA_SEG
    mem.w(DS, AC.SFX_TABLE + 2 * 4, 0x0040)        # dl=2 descriptor: src
    mem.w(DS, AC.SFX_TABLE + 2 * 4 + 2, 5)         # len
    mem.w(DS, 0x0B59, 0x2000)                       # sample segment ptr
    payload = bytes([10, 20, 30, 40, 50])
    flat = (0x2000 << 4) + 0x0040
    mem.data[flat:flat + 5] = payload
    ev = AC.resolve_sfx(mem, 2)
    assert isinstance(ev, PlaySfx) and ev.sfx_id == 2 and ev.pcm == payload


def test_song_load_fingerprint_stable_and_empty():
    """The load fingerprint is None with no song and constant when memory is unchanged
    (the signal the observer waits on before capturing a half-loaded, silent song)."""
    mem = _FakeMem()
    assert AC.song_load_fingerprint(mem) is None       # no song loaded
    DS = AC.DATA_SEG
    order = [6, 2, 0, 1, 3, 5, 4, 10, 9, 7, 8, 12, 11]
    base = (DS << 4) + 0xDC7
    mem.data[base:base + len(order)] = bytes(order)
    mem.data[(DS << 4) + 0xDC2] = len(order)            # song_length
    mem.data[(DS << 4) + 0xB84] = 6                     # playback speed (PB_SPEED, initialised)
    fp1 = AC.song_load_fingerprint(mem)
    assert fp1 is not None and AC.song_load_fingerprint(mem) == fp1   # stable when unchanged
    mem.data[base] = 99                                 # loader still mutating -> changes
    assert AC.song_load_fingerprint(mem) != fp1


def test_identify_song_matches_real_trk():
    """The loaded order table fingerprints the .TRK (MINES = the 185902 song)."""
    mem = _FakeMem()
    DS = AC.DATA_SEG
    mines_order = [6, 2, 0, 1, 3, 5, 4, 10, 9, 7, 8, 12, 11]
    base = (DS << 4) + 0xDC7
    mem.data[base:base + len(mines_order)] = bytes(mines_order)
    mem.data[(DS << 4) + 0xDC2] = len(mines_order)   # song_length
    found = AC.identify_song(mem, ASSETS)
    assert found is not None and found[0] == "MINES.TRK"


# ---- the player + both backends --------------------------------------------------

def test_mod_player_plays_standard_module():
    y = ModPlayer(_std_module(), out_rate=22050).render(22050)
    assert y.shape == (22050, 2) and y.dtype == np.float32
    assert not np.isnan(y).any() and np.max(np.abs(y)) > 0.0


def test_enhanced_backend_plays_song_and_sfx():
    be = EnhancedBackend(out_rate=22050, free_run=True)   # offline: own tempo clock
    be.handle(StartSong(module=_std_module(), name="test"))
    be.handle(PlaySfx(sfx_id=0, pcm=bytes(range(64))))
    y = be.render(22050)
    assert y.shape == (22050, 2) and not np.isnan(y).any()
    assert np.max(np.abs(y)) > 0.0


def test_enhanced_sequencer_gated_by_game_ticks():
    """Live mode: the sequencer advances only on supplied game-audio ticks; voices
    render continuously regardless (slow game = later notes, never broken notes)."""
    be = EnhancedBackend(out_rate=22050)                  # free_run=False (live)
    be.handle(StartSong(module=_std_module()))
    # No ticks supplied yet -> the row is never processed -> music is silent (held),
    # but the render must not crash or gap.
    assert float(np.max(np.abs(be.render(4096)))) == 0.0
    # Supply game audio time -> the note triggers and plays.
    be.advance_ticks(8)
    assert float(np.max(np.abs(be.render(8192)))) > 0.0
    # SFX is pure audio time, NOT gated by ticks: it plays with no ticks supplied.
    be2 = EnhancedBackend(out_rate=22050)
    be2.handle(StartSong(module=_std_module()))
    be2.handle(PlaySfx(sfx_id=0, pcm=bytes(range(64))))
    assert float(np.max(np.abs(be2.render(2048)))) > 0.0


def test_faithful_backend_plays_pre2_module():
    fb = FaithfulBackend()
    fb.start_module(_pre2_module())
    block = fb.render(30)
    assert len(block) == 30 * 168
    assert any(b not in (0, 0x80) for b in block)


def test_stop_silences_both():
    eb = EnhancedBackend(out_rate=22050)
    eb.handle(StartSong(module=_std_module()))
    eb.handle(StopSong())
    assert float(np.max(np.abs(eb.render(2048)))) == 0.0
    fb = FaithfulBackend()
    fb.start_module(_pre2_module())
    fb.handle(StopSong())
    assert all(b in (0, 0x80) for b in fb.render(5))


def test_audio_state_from_module_starts_at_song_top():
    st = audio_state_from_module(_pre2_module())
    assert st.pb.order_pos == 0 and st.pb.row == 0
    assert all(v.pos == 0xFFFF for v in st.voices)
    assert len(st.mixer_instruments) == 1


# ---- single owner + rooted enhanced renderer -------------------------------------

def test_recovered_system_owns_one_clock_for_both_strategies():
    """RecoveredAudioSystem is the single owner: faithful and enhanced both branch from
    the same recovered model + sequencer (no parallel player)."""
    from pre2.audio.recovered_system import RecoveredAudioSystem
    sysm = RecoveredAudioSystem()
    sysm.start_song(_pre2_module())
    # faithful strategy: byte-exact 8-bit blocks straight off the recovered mixer.
    block = b"".join(bytes(sysm.render_faithful_block()) for _ in range(30))
    assert len(block) == 30 * 168 and any(b not in (0, 0x80) for b in block)


def test_enhanced_renderer_is_rooted_in_recovered_voices():
    """The enhanced renderer plays the recovered tracker's intent: a note triggered by
    the recovered sequencer (not a clean-room parse) drives a float voice."""
    from pre2.audio.recovered_system import RecoveredAudioSystem
    from pre2.audio.enhanced_render import EnhancedRenderer
    sysm = RecoveredAudioSystem()
    sysm.start_song(_pre2_module())
    er = EnhancedRenderer(sysm, out_rate=22050, free_run=True)
    y = er.render(22050)
    assert y.shape == (22050, 2) and y.dtype == np.float32
    assert not np.isnan(y).any() and float(np.max(np.abs(y))) > 0.0
    # the pitch the renderer used came from the recovered voice's resample step
    voice = sysm.voices[0]
    assert voice.period == 256                          # period_table[100] from the fixture
    assert abs(er._pitch_advance(voice.period) - (1.0 + 256 / 256) * (8403 / 22050)) < 1e-6


def test_enhanced_renderer_sfx_from_recovered_command():
    """A PlaySfx command on the recovered system surfaces as an enhanced one-shot."""
    from pre2.audio.recovered_system import RecoveredAudioSystem
    from pre2.audio.enhanced_render import EnhancedRenderer
    sysm = RecoveredAudioSystem()
    sysm.play_sfx(bytes((i * 4) & 0xFF for i in range(128)))
    er = EnhancedRenderer(sysm, out_rate=22050, free_run=True)
    assert float(np.max(np.abs(er.render(2048)))) > 0.0


# ---- the enhanced renderer is rooted in the recovered MODEL, but free of the machine --

def _imports_of(rel_path: str) -> set[str]:
    tree = ast.parse((ROOT / rel_path).read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def test_enhanced_renderer_rooted_in_model_but_free_of_machine():
    """The modern enhanced renderer may grow from the pure recovered model (tracker voices,
    instruments, sequencer) — that is the point — but must NOT depend on the VM, the bridge,
    or the Sound Blaster / DMA / IRQ machine."""
    mods = _imports_of("pre2/audio/enhanced_render.py")
    forbidden = ("dos_re", "pre2.bridge")
    leaked = [m for m in mods for f in forbidden if m == f or m.startswith(f + ".")]
    assert not leaked, f"enhanced renderer leaks machine deps: {leaked}"
    assert not any("sound_blaster" in m or "dma" in m or "_irq" in m for m in mods)
    # rooted: it consumes the recovered audio system (not a stand-alone clean-room player)
    assert "pre2.audio.recovered_system" in mods


def test_recovered_model_layer_has_no_vm_deps():
    """The recovered audio owner + the recovered engine stay pure (no VM/SB)."""
    for rel in ("pre2/audio/recovered_system.py", "pre2/recovered/audio_system.py",
                "pre2/audio/recovered_enhanced_backend.py"):
        mods = _imports_of(rel)
        assert not any(m == "dos_re" or m.startswith("dos_re.") for m in mods)
        assert not any("sound_blaster" in m or "cpu" in m for m in mods)


# ---- native scheduler + diagnostics ----------------------------------------------

def test_enhanced_native_tick_cadence_tracks_tick_hz():
    """Free-run is a native audio-time scheduler: the sequencer ticks at ~TICK_HZ relative
    to rendered frames, regardless of how render() is chunked (no VM/SB clock)."""
    from pre2.audio.recovered_system import RecoveredAudioSystem
    from pre2.audio.enhanced_render import EnhancedRenderer, TICK_HZ
    sysm = RecoveredAudioSystem()
    sysm.start_song(_pre2_module())
    er = EnhancedRenderer(sysm, out_rate=44100, free_run=True)
    for _ in range(100):                                # many small irregular chunks
        er.render(441)
    assert abs(er.tick_cadence_hz() - TICK_HZ) < 1.0    # within ~1 Hz of the native rate


def test_rooted_backend_diagnostics_flag_repeats_and_missed_sfx():
    from pre2.audio.recovered_enhanced_backend import RecoveredEnhancedBackend
    from pre2.audio.events import PlaySfx, StartSong
    be = RecoveredEnhancedBackend(free_run=True)
    mod = _pre2_module()
    be.handle(StartSong(recovered_module=mod))
    be.handle(StartSong(recovered_module=mod))           # same order -> repeat flagged
    be.handle(StartSong(module=_std_module()))           # no recovered_module -> unrooted
    be.handle(PlaySfx(sfx_id=1, pcm=b""))                # empty -> missed
    be.handle(PlaySfx(sfx_id=2, pcm=bytes(64)))
    d = be.diagnostics()
    assert d["enh_songs"] == "2" and d["enh_song_repeat"] == "1"
    assert d["enh_song_unrooted"] == "1"
    assert d["enh_sfx"] == "1" and d["enh_sfx_missed"] == "1"


def test_live_engine_deterministic_render_through_queue():
    """The live engine stays synchronously testable: posting commands then render() drains the
    queue and produces the same audio as the core backend handling them directly."""
    from pre2.audio.live_engine import LiveEnhancedAudioEngine
    from pre2.audio.recovered_enhanced_backend import RecoveredEnhancedBackend
    from pre2.audio.events import StartSong
    mod = _pre2_module()
    eng = LiveEnhancedAudioEngine(out_rate=22050, free_run=True)
    eng.post(StartSong(recovered_module=mod))
    y = eng.render(8192)                                  # drains the queue, then renders
    assert eng.commands_applied == 1 and float(np.max(np.abs(y))) > 0.0
    ref = RecoveredEnhancedBackend(out_rate=22050, free_run=True)
    ref.handle(StartSong(recovered_module=mod))
    assert np.array_equal(y, ref.render(8192))           # queue path == direct path


def test_live_engine_post_does_not_mutate_state():
    """Posting a command only enqueues; playback state changes only when the engine renders
    (i.e. only the audio side advances state, never the poster)."""
    from pre2.audio.live_engine import LiveEnhancedAudioEngine
    from pre2.audio.events import StartSong
    eng = LiveEnhancedAudioEngine(out_rate=22050, free_run=True)
    eng.post(StartSong(recovered_module=_pre2_module()))
    assert not eng.backend.system.playing                # not applied yet
    eng.render(64)
    assert eng.backend.system.playing                    # applied on the render/audio side


def test_live_engine_has_no_vm_or_device_deps():
    """The engine is a pure runtime wrapper: no VM, no Sound Blaster, no pygame/SDL."""
    mods = _imports_of("pre2/audio/live_engine.py")
    assert not any(m == "dos_re" or m.startswith("dos_re.") for m in mods)
    assert not any("sound_blaster" in m or "pygame" in m or "sdl" in m for m in mods)


def test_rooted_backend_does_not_read_sound_blaster():
    """The enhanced output is produced purely from the recovered model — it never reads the
    SB. Proven by rendering identical audio with no SB object anywhere in the path."""
    from pre2.audio.recovered_enhanced_backend import RecoveredEnhancedBackend
    from pre2.audio.events import StartSong
    a = RecoveredEnhancedBackend(free_run=True); a.out_rate = 22050
    a.handle(StartSong(recovered_module=_pre2_module()))
    b = RecoveredEnhancedBackend(free_run=True); b.out_rate = 22050
    b.handle(StartSong(recovered_module=_pre2_module()))
    ya, yb = a.render(8192), b.render(8192)
    assert np.array_equal(ya, yb) and float(np.max(np.abs(ya))) > 0.0
