"""Tests for the semantic-command audio architecture (command layer + faithful oracle).

Synthetic fixtures only (plus the repo's real ``.TRK`` assets for song identification).
The byte-exact fidelity of the faithful path vs the ISR oracle is covered separately by
``pre2/probes/verify_audio_system.py``. The live enhanced path (SDL_mixer) is exercised by
``scripts/play.py --audio enhanced``, not here.
"""
from __future__ import annotations

import ast
from pathlib import Path

import numpy as np

from pre2.audio.assets import Module, SampleAsset
from pre2.audio.events import PlaySfx, StopSong
from pre2.audio.faithful_backend import FaithfulBackend, audio_state_from_module
from pre2.bridge import audio_commands as AC

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


# ---- the faithful oracle backend -------------------------------------------------

def test_faithful_backend_plays_pre2_module():
    fb = FaithfulBackend()
    fb.start_module(_pre2_module())
    block = fb.render(30)
    assert len(block) == 30 * 168
    assert any(b not in (0, 0x80) for b in block)


def test_faithful_backend_stop_silences():
    fb = FaithfulBackend()
    fb.start_module(_pre2_module())
    fb.handle(StopSong())
    assert all(b in (0, 0x80) for b in fb.render(5))


def test_audio_state_from_module_starts_at_song_top():
    st = audio_state_from_module(_pre2_module())
    assert st.pb.order_pos == 0 and st.pb.row == 0
    assert all(v.pos == 0xFFFF for v in st.voices)
    assert len(st.mixer_instruments) == 1


# ---- the faithful recovered engine stays pure (no VM / Sound Blaster) ------------

def _imports_of(rel_path: str) -> set[str]:
    tree = ast.parse((ROOT / rel_path).read_text(encoding="utf-8"))
    mods: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    return mods


def test_faithful_engine_has_no_vm_deps():
    """The recovered faithful engine (the oracle) stays VM-independent: no cpu/dos_re/SB."""
    mods = _imports_of("pre2/recovered/audio_system.py")
    assert not any(m == "dos_re" or m.startswith("dos_re.") for m in mods)
    assert not any("sound_blaster" in m or m.endswith(".cpu") for m in mods)
