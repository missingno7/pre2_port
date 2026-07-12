"""The field-backed seam's guards: the generated registry never drifts from the views, the serializer
round-trips, and the shipped FieldBackend agrees with ByteBackend and fails loud outside the declarations."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "scripts"))


def test_registry_is_current():
    """pre2/bridge/field_registry.py must match what gen_field_registry harvests from the views RIGHT NOW —
    if a view field changed, regenerate: python scripts/gen_field_registry.py"""
    import gen_field_registry as gen

    rows, arenas = gen.harvest()
    expected = gen.render(rows, arenas)
    actual = gen.OUT.read_text(encoding="utf-8")
    assert actual == expected, "field_registry.py drifted from the views -- run scripts/gen_field_registry.py"


def test_registry_covers_only_dgroup():
    from pre2.bridge.field_registry import ARENAS, FIELDS

    for name, (off, width) in FIELDS.items():
        assert 0 <= off and off + width <= 0x10000, name
        assert width in (1, 2), name
    for label, (lo, hi) in ARENAS.items():
        assert 0 <= lo <= hi < 0x10000, label


def test_serializer_roundtrip():
    """image -> fields -> image is byte-identical over the named region (on a synthetic patterned image)."""
    from pre2.bridge.state_fields import apply_fields, fields_from_image, named_bytes
    from pre2.views.dgroup_view import DGROUP_BASE

    img = bytearray(DGROUP_BASE + 0x10000)
    for o in range(0x10000):
        img[DGROUP_BASE + o] = (o * 7 + (o >> 8)) & 0xFF
    rebuilt = bytearray(len(img))
    apply_fields(rebuilt, fields_from_image(img))
    for o in named_bytes():
        assert rebuilt[DGROUP_BASE + o] == img[DGROUP_BASE + o], hex(o)


def test_fieldbackend_equivalence_and_fail_loud():
    from pre2.bridge.field_registry import FIELDS
    from pre2.views.dgroup_view import DGROUP_BASE, ByteBackend, FieldBackend

    img = bytearray(DGROUP_BASE + 0x10000)
    for o in range(0x10000):
        img[DGROUP_BASE + o] = (o * 13 + 5) & 0xFF
    bb, fb = ByteBackend(img), FieldBackend(img)
    for name, (off, width) in FIELDS.items():
        if width == 2:
            assert fb.rw(off) == bb.rw(off), name
        else:
            assert fb.rb(off) == bb.rb(off), name
    # writes land and read back
    off, width = FIELDS["PlayerGlobals.energy"]
    fb.wb(off, 0x42)
    assert fb.rb(off) == 0x42
    # fail-loud outside every declaration (0xFFFE is untouched top-of-segment)
    with pytest.raises(KeyError):
        fb.rb(0xFFFE)
    with pytest.raises(KeyError):
        FieldBackend().wb(0xFFFE, 1)


def test_fieldbackend_view_binding():
    """A named view bound to a FieldBackend works with no memory image at all."""
    from pre2.views.dgroup_view import FieldBackend, PlayerGlobals

    g = PlayerGlobals(FieldBackend())
    g.energy = 3
    g.lives = 2
    assert (g.energy, g.lives) == (3, 2)
    assert g.score_lo == 0                       # unwritten named state reads as zero


def test_named_diff():
    from pre2.bridge.state_fields import named_diff
    from pre2.views.dgroup_view import DGROUP_BASE, ByteBackend, PlayerGlobals

    a = bytearray(DGROUP_BASE + 0x10000)
    b = bytearray(a)
    PlayerGlobals(ByteBackend(b)).energy = 5
    diff = named_diff(a, b)
    assert any(n.endswith(".energy") and va == 0 and vb == 5 for n, va, vb in diff)
