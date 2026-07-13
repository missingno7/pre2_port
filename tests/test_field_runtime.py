"""The field-backed runtime (the detachable-umbilical seam): gameplay mode <-> image mode, and the
materialize fold. The whole-lifecycle byte-exact proof against the VM lives in scripts/verify_hybrid_finish.py."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pre2.native.field_runtime import enter_image_mode, is_field_backed, materialize, to_field_store
from pre2.native.state import NativeGameState
from pre2.views.dgroup_view import ByteBackend, HybridBackend, PlayerGlobals

DGROUP = 0x1A0F << 4


def _state():
    data = bytearray(0x100000)
    for o in range(0x10000):
        data[DGROUP + o] = (o * 5 + 1) & 0xFF
    return NativeGameState(data)


def test_default_is_image_backed():
    s = _state()
    assert isinstance(s.backend, ByteBackend)
    assert not is_field_backed(s)


def test_to_field_store_then_gameplay_writes_named_off_image():
    s = _state()
    to_field_store(s)
    assert is_field_backed(s) and isinstance(s.backend, HybridBackend)
    before = s.data[DGROUP + 0x27D6]
    PlayerGlobals(s).energy = (before ^ 0xFF) & 0xFF      # a named write
    assert s.data[DGROUP + 0x27D6] == before              # the image is untouched (state lives in the field store)
    assert PlayerGlobals(s).energy == (before ^ 0xFF) & 0xFF


def test_materialize_folds_named_state_into_the_image():
    s = _state()
    to_field_store(s)
    PlayerGlobals(s).energy = 3
    PlayerGlobals(s).lives = 7
    materialize(s)
    assert s.data[DGROUP + 0x27D6] == 3 and s.data[DGROUP + 0x27D8] == 7   # now visible in the image


def test_enter_image_mode_preserves_state_and_switches_backend():
    s = _state()
    to_field_store(s)
    PlayerGlobals(s).energy = 2                            # a named write, off the image
    enter_image_mode(s)
    assert isinstance(s.backend, ByteBackend)              # now image-backed
    assert s.data[DGROUP + 0x27D6] == 2                    # the named write was materialised, not lost
    assert PlayerGlobals(s).energy == 2                    # and readable through the image now


def test_round_trip_field_image_field_is_lossless():
    s = _state()
    original = bytes(s.data[DGROUP:DGROUP + 0x10000])
    to_field_store(s)                                     # image -> field store
    materialize(s)                                        # field store -> image
    assert bytes(s.data[DGROUP:DGROUP + 0x10000]) == original
