"""Tests for the two-layer audio architecture (semantic events -> faithful/enhanced).

Synthetic fixtures only (no snapshot): a tiny one-note module + a fake memory image
for the SFX descriptor resolver. The byte-exact fidelity of the faithful path vs the
ISR oracle is covered separately by ``pre2/probes/verify_audio_system.py``.
"""
from __future__ import annotations

import ast
from pathlib import Path

import numpy as np

from pre2.audio.assets import Module, SampleAsset
from pre2.audio.enhanced_backend import EnhancedBackend
from pre2.audio.events import (
    PlaySfx, SetMusicEnabled, StartSong, StopSong,
)
from pre2.audio.faithful_backend import FaithfulBackend, audio_state_from_module
from pre2.bridge import audio_commands as AC


# ---- fixtures --------------------------------------------------------------------

def _one_note_module() -> Module:
    """A 1-sample, 1-pattern module: row 0 ch 0 triggers sample 1 at period 100."""
    pat = bytearray(1024)
    # cell format: sample_num = (b2>>4)|(b1&0x10); period = b0|(b1<<8) & 0x7FFF
    pat[0:4] = bytes([100, 0, 0x10, 0])        # sample 1, period 100, no effect
    period_table = [0] * 0x8000
    period_table[100] = 256                     # step 256 -> ~source_rate playback
    ramp = bytes((i * 2) & 0xFF for i in range(300))
    sample = SampleAsset(pcm=ramp, length=200, loop_start=0, loop_len=0, default_volume=64)
    # the faithful mixer scales every sample through vol_table; a ramp keeps it audible
    vol_table = bytes(i & 0xFF for i in range(65 * 64 + 256))
    return Module(order=(0,), song_length=0, patterns={0: bytes(pat)},
                  samples=(sample,), period_table=tuple(period_table),
                  vol_table=vol_table, initial_speed=3)


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
    # descriptor for dl=2 at DS:0x1009 + 2*4 -> {src, len}
    mem.w(DS, AC.SFX_TABLE + 2 * 4, 0x0040)        # src offset
    mem.w(DS, AC.SFX_TABLE + 2 * 4 + 2, 5)         # length
    mem.w(DS, 0x0B59, 0x2000)                       # sample segment ptr ([0xb59])
    payload = bytes([10, 20, 30, 40, 50])
    flat = (0x2000 << 4) + 0x0040
    mem.data[flat:flat + 5] = payload

    ev = AC.resolve_sfx(mem, 2)
    assert isinstance(ev, PlaySfx)
    assert ev.sfx_id == 2
    assert ev.pcm == payload


# ---- both backends consume the same events --------------------------------------

def test_faithful_backend_plays_module():
    be = FaithfulBackend()
    be.handle(StartSong(module=_one_note_module()))
    block = be.render(30)               # 30 blocks past the initial_speed delay
    assert len(block) == 30 * 168
    assert any(b not in (0, 0x80) for b in block)   # the note sounds


def test_enhanced_backend_plays_module():
    be = EnhancedBackend(out_rate=22050)
    be.handle(StartSong(module=_one_note_module()))
    y = be.render(22050)               # 1 second
    assert y.dtype == np.float32 and y.shape == (22050,)
    assert not np.isnan(y).any()
    assert np.max(np.abs(y)) > 0.0     # the note sounds


def test_backends_are_interchangeable_for_the_event_stream():
    events = [StartSong(module=_one_note_module()),
              PlaySfx(sfx_id=0, pcm=bytes(range(64))),
              SetMusicEnabled(True)]
    fb, eb = FaithfulBackend(), EnhancedBackend(out_rate=22050)
    for ev in events:                  # neither backend raises on any event type
        fb.handle(ev)
        eb.handle(ev)
    assert len(fb.render(5)) == 5 * 168
    assert eb.render(2048).shape == (2048,)


def test_stop_song_silences_both():
    fb, eb = FaithfulBackend(), EnhancedBackend(out_rate=22050)
    for be in (fb, eb):
        be.handle(StartSong(module=_one_note_module()))
        be.handle(StopSong())
    assert all(b in (0, 0x80) for b in fb.render(5))
    assert float(np.max(np.abs(eb.render(2048)))) == 0.0


def test_audio_state_from_module_starts_at_song_top():
    st = audio_state_from_module(_one_note_module())
    assert st.pb.order_pos == 0 and st.pb.row == 0
    assert all(v.pos == 0xFFFF for v in st.voices)     # all voices start silent
    assert len(st.mixer_instruments) == 1


# ---- the enhanced backend must not depend on DOS/SB/VM internals ----------------

def test_enhanced_backend_has_no_vm_or_mixer_deps():
    src = Path(__file__).resolve().parents[1] / "pre2" / "audio" / "enhanced_backend.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    forbidden = ("dos_re", "pre2.recovered.mixer", "pre2.recovered.audio_system",
                 "pre2.bridge")
    leaked = [m for m in mods for f in forbidden if m == f or m.startswith(f + ".")]
    assert not leaked, f"enhanced backend leaks low-level deps: {leaked}"
    # cpu/mem/SoundBlaster/DMA/ISR concepts must not appear
    assert not any("sound_blaster" in m or "dma" in m for m in mods)
