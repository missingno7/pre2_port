"""NativeAudio.poll boss-music switching — the 7585 song-request seam (state.song_request) drives a one-shot
``native_load_song`` + a fingerprint-gated StartSong. Regression: the per-frame de-dup latch (``_req_file``)
must not permanently suppress a LATER re-request of the same song after an intervening external level-song load
(else the boss music never switches back — user-reported "gorilla boss music not switching").

Mocked at the load/fingerprint boundary so it needs no .TRK assets on disk: a shared ``order`` box stands in
for the DGROUP order table; ``native_load_song`` writes it, ``song_load_fingerprint`` reads it, and
``make_start_song`` echoes it as the emitted command's ``name``.
"""
from __future__ import annotations

import pytest

import pre2.native.audio as audio


class _Cmd:
    def __init__(self, name):
        self.name = name


class _State:
    """Minimal stand-in: poll only touches ``song_request`` and (via the patched helpers) the order box."""
    def __init__(self):
        self.song_request = None
        self.sfx_queue = None


@pytest.fixture
def au(monkeypatch):
    box = {"order": None}                                    # the (mocked) DGROUP order table

    def _load(state, name, root):                           # native_load_song: an external OR req-driven load
        box["order"] = name

    monkeypatch.setattr(audio, "native_load_song", _load)
    monkeypatch.setattr(audio, "song_load_fingerprint", lambda state: box["order"])
    monkeypatch.setattr(audio, "make_start_song", lambda state, root: _Cmd(box["order"]))

    emitted = []
    a = audio.NativeAudio(emitted.append, game_root="/unused")
    a._emitted = emitted
    a._box = box
    return a


def _last(a):
    return a._emitted[-1].name if a._emitted else None


def test_boss_song_reswitches_after_intervening_level_song(au):
    st = _State()

    # a prior level song is playing (external load, like a level start)
    audio.native_load_song(st, "MINES.TRK", "/unused")
    au.poll(st)
    assert _last(au) == "MINES.TRK"

    # first boss encounter -> the 7585 seam requests MONSTER every frame; it loads + emits ONCE
    st.song_request = 13
    au.poll(st)
    assert _last(au) == "MONSTER.TRK"
    n_after_boss1 = len(au._emitted)
    st.song_request = 13                                    # same request next frame -> de-duped (no reload/emit)
    au.poll(st)
    assert len(au._emitted) == n_after_boss1

    # the next level loads its own song externally (overwrites the order table)
    audio.native_load_song(st, "GLACE.TRK", "/unused")
    au.poll(st)
    assert _last(au) == "GLACE.TRK"

    # SECOND boss encounter, SAME index -> MUST switch back to MONSTER (the bug left it stuck on GLACE)
    st.song_request = 13
    au.poll(st)
    assert _last(au) == "MONSTER.TRK"


def test_repeated_boss_request_without_external_load_stays_deduped(au):
    """A boss holds song_request for many frames with no external load in between: load + emit exactly once."""
    st = _State()
    for _ in range(5):
        st.song_request = 13
        au.poll(st)
    assert _last(au) == "MONSTER.TRK"
    assert len(au._emitted) == 1                            # not re-loaded/re-emitted every frame
