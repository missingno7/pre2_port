"""The field-backed seam's bridge-side serializer (step 5): DGROUP image <-> named-field dict.

The shipped game's state substrate stays the DGROUP byte image (it is the game's heap — record pointers,
script cursors and back-refs are offsets stored IN game state, so the image is semantically load-bearing).
What the flip delivers is that every MUTABLE byte of it is covered by a NAME (the cartography's
WRITTEN-unnamed count is zero), which makes this lossless translation possible:

    fields_from_image(data)          -> {name: value} + {arena: bytes}   (a human-readable snapshot)
    apply_fields(data, fields)       -> writes a named snapshot back into an image
    named_diff(a, b)                 -> [(name, val_a, val_b)]           (two images, field-level diff)

Workbench uses: named save-state/snapshot dumps, field-level regression diffs (instead of raw byte offsets),
and the flip proof itself (scripts/verify_field_flip.py: per-tick round-trip identity + mutation coverage).

The registry (pre2/bridge/field_registry.py) is MACHINE-GENERATED from the shipped views — regenerate with
scripts/gen_field_registry.py after any view change. Aliased names (two widths over one byte) are always
mutually consistent when extracted from one image, so apply order never matters.
"""
from __future__ import annotations

from pre2.bridge.field_registry import ARENAS, FIELDS
from pre2.views.dgroup_view import DGROUP_BASE


def _rd(data, off: int, width: int) -> int:
    b = DGROUP_BASE + (off & 0xFFFF)
    return data[b] if width == 1 else data[b] | (data[b + 1] << 8)


def fields_from_image(data) -> dict:
    """Extract every named field (and arena, as ``bytes``) from a 1 MB image / NativeGameState ``.data``."""
    data = getattr(data, "data", data)
    out = {name: _rd(data, off, width) for name, (off, width) in FIELDS.items()}
    for label, (lo, hi) in ARENAS.items():
        out[label] = bytes(data[DGROUP_BASE + lo:DGROUP_BASE + hi + 1])
    return out


def apply_fields(data, fields: dict) -> None:
    """Write a named snapshot back into an image (only the names present in ``fields``)."""
    data = getattr(data, "data", data)
    for name, val in fields.items():
        if name in FIELDS:
            off, width = FIELDS[name]
            b = DGROUP_BASE + off
            data[b] = val & 0xFF
            if width == 2:
                data[b + 1] = (val >> 8) & 0xFF
        elif name in ARENAS:
            lo, hi = ARENAS[name]
            if len(val) != hi - lo + 1:
                raise ValueError(f"arena {name!r}: expected {hi - lo + 1} bytes, got {len(val)}")
            data[DGROUP_BASE + lo:DGROUP_BASE + hi + 1] = val
        else:
            raise KeyError(f"unknown field {name!r} (regenerate the registry?)")


def named_bytes() -> set[int]:
    """Every DGROUP offset the registry covers (fields + arenas) — the flip's named region."""
    s = set()
    for off, width in FIELDS.values():
        s.update((off + k) & 0xFFFF for k in range(width))
    for lo, hi in ARENAS.values():
        s.update(range(lo, hi + 1))
    return s


def named_diff(a, b) -> list[tuple[str, object, object]]:
    """Field-level diff of two images: [(name, value_in_a, value_in_b)] for every differing name."""
    fa, fb = fields_from_image(a), fields_from_image(b)
    return [(n, fa[n], fb[n]) for n in fa if fa[n] != fb[n]]
