"""The field-backed flip's store (Phase 4): HybridBackend keeps named DGROUP bytes in a FieldBackend and
everything else in a residue image, presents the offset-keyed backend API, and round-trips losslessly."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pre2.views.dgroup_view import (DGROUP_BASE, ByteBackend, FieldBackend, HybridBackend, PlayerGlobals,
                                    _named_map)


def _image():
    img = bytearray(DGROUP_BASE + 0x10000)
    for o in range(0x10000):
        img[DGROUP_BASE + o] = (o * 7 + 3) & 0xFF
    return img


def test_reads_split_named_vs_residue():
    img = _image()
    fb = FieldBackend(img)
    hb = HybridBackend(fb, img)
    named = _named_map()
    # a named offset and a residue offset
    named_off = next(iter(named))
    residue_off = next(o for o in range(0x10000) if o not in named)
    assert hb.rb(named_off) == fb.rb(named_off) == img[DGROUP_BASE + named_off]
    assert hb.rb(residue_off) == img[DGROUP_BASE + residue_off]


def test_named_writes_land_in_fields_not_image():
    img = _image()
    fb = FieldBackend(img)
    hb = HybridBackend(fb, img)
    off = next(iter(_named_map()))
    before = img[DGROUP_BASE + off]
    hb.wb(off, (before ^ 0xFF) & 0xFF)
    assert fb.rb(off) == (before ^ 0xFF) & 0xFF          # the field took it
    assert img[DGROUP_BASE + off] == before              # the image is untouched (named bytes go stale)
    assert hb.rb(off) == (before ^ 0xFF) & 0xFF          # ... and reads back through the field


def test_residue_writes_land_in_image():
    img = _image()
    fb = FieldBackend(img)
    hb = HybridBackend(fb, img)
    off = next(o for o in range(0x10000) if o not in _named_map())
    hb.wb(off, 0x5A)
    assert img[DGROUP_BASE + off] == 0x5A


def test_materialize_round_trip():
    img = _image()
    fb = FieldBackend(img)
    hb = HybridBackend(fb, img)
    # mutate some named fields through the hybrid, then materialise and confirm the image is whole again
    for off in list(_named_map())[:200]:
        hb.wb(off, (off * 3) & 0xFF)
    scratch = bytearray(img)                              # a copy with stale named bytes
    hb.materialize(scratch)
    for off in _named_map():
        assert scratch[DGROUP_BASE + off] == fb.rb(off), hex(off)


def test_view_over_hybrid():
    """A named view bound to the hybrid reads/writes the field store — no image needed for named state."""
    img = _image()
    fb = FieldBackend(img)
    g = PlayerGlobals(HybridBackend(fb, img))
    g.energy = 2
    g.lives = 7
    assert (g.energy, g.lives) == (2, 7)
    assert fb.rb(0x27D6) == 2                             # energy landed in the field store


def test_bytebackend_equivalence_seed():
    """Seeded from an image, the hybrid reads every named field identically to a ByteBackend over that image."""
    from pre2.bridge.field_registry import FIELDS
    img = _image()
    bb, hb = ByteBackend(img), HybridBackend(FieldBackend(img), img)
    for name, (off, width) in FIELDS.items():
        if width == 2:
            assert hb.rw(off) == bb.rw(off), name
        else:
            assert hb.rb(off) == bb.rb(off), name
