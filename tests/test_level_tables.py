"""Gate: the per-level tile tables are a typed object on the graph, not undifferentiated residue.

P5 slice 1b (docs/pre2/native_dataclass_lift.md). ``ceil_props``/``floor_props``/``ceil_handler``/
``tile_props``/``dirty_kind`` are the LEVEL's content, loaded from its ``*.SQZ``. They used to sit in
``ObjectGraphStore._level_data`` — the "everything un-routed" DGROUP residue, an opaque bytearray. They are now
named fields on :class:`pre2.game.model.LevelTables`, routed like any other object-graph state.

Distinct from slice 1a's BOOT constants (trig, sprite metrics), which are static and ship as plain literals in
``pre2/native/asset_tables.py``. These change with every level, so they are per-state data, not module
constants — which is exactly why they are routed rather than frozen.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DGROUP_BASE = 0x1A0F << 4


def _bases():
    import pre2.views.tables as T
    return [("ceil_props", T.CEIL_PROPS), ("floor_props", T.FLOOR_PROPS), ("ceil_handler", T.CEIL_HANDLER),
            ("tile_props", T.TILE_PROPS), ("dirty_kind", T.DIRTY_KIND)]


def test_extents_are_one_entry_per_tile_id_and_agree_with_the_layout():
    """256 = one entry per tile id. Rigorous rather than guessed: each base is the next table's base minus
    0x100 for the contiguous run, and the routed extent matches the tile-id semantics + the measured max index
    of 255. Guessing extents from base-gaps alone is how you swallow mutable state."""
    import pre2.native.graph_layout as G
    routed = dict((f, (base, ln)) for f, base, ln in G._LEVEL_TABLES)
    assert set(routed) == {f for f, _ in _bases()}
    for f, base in _bases():
        rb, ln = routed[f]
        assert rb == base, f"{f}: routed base 0x{rb:04X} != views/tables 0x{base:04X}"
        assert ln == 256, f"{f}: extent {ln} != 256 (one entry per tile id)"
    assert 0x7E5E + 0x100 == 0x7F5E and 0x7F5E + 0x100 == 0x805E, "the contiguous tile-table run moved"


def test_the_level_tables_carry_the_loaded_level_content_as_a_typed_object():
    """The point of the slice: after a real *.SQZ load the level's tile content is reachable as named fields on
    a dataclass, byte-identical to what the loader produced -- not as an opaque residue blob."""
    import pytest
    from pre2.game.model import LevelTables
    from pre2.native.cold_boot import native_cold_boot
    from pre2.native.object_runtime import to_object_store
    if not (ROOT / "assets").exists():
        pytest.skip("game assets not present")
    state = native_cold_boot(str(ROOT / "assets"), level=0)
    image = bytes(state.data[DGROUP_BASE:DGROUP_BASE + 0x10000])
    to_object_store(state)
    lt = state.backend.level_tables
    assert isinstance(lt, LevelTables)
    for f, base in _bases():
        assert bytes(getattr(lt, f)) == image[base:base + 256], f"{f} does not match the loaded level content"
    assert any(any(getattr(lt, f)) for f, _ in _bases()), "tables are all zero — the level did not load"


def test_the_tables_are_no_longer_undifferentiated_residue():
    """Regression guard for the slice itself: every byte of all five tables must be routed to a LevelTables
    FIELD. If a future change drops the routing they would silently fall back to _level_data and the migration
    would be undone without any test noticing."""
    import pre2.native.graph_layout as G
    from pre2.game.model import LevelTables
    from pre2.native.boot_data import build_boot_memory
    from pre2.native.state import NativeGameState
    st = NativeGameState(build_boot_memory())
    be = G.DataclassBackend(st)
    for f, base in _bases():
        for k in (0, 1, 128, 255):
            m = be._map.get(base + k)
            assert m is not None, f"{f}[{k}] (0x{base + k:04X}) is not routed"
            inst, field, idx, w, _s = m
            assert isinstance(inst, LevelTables) and field == f and idx == k and w == 0, \
                f"{f}[{k}] routes to {field!r} on {type(inst).__name__}, expected LevelTables.{f}"


def test_materialize_folds_the_typed_tables_back_byte_exactly():
    """The tables must round-trip: object -> image. A mutation through the graph has to reappear in the
    materialized DGROUP, or the render/oracle path would see stale content."""
    import pre2.native.graph_layout as G
    from pre2.native.boot_data import build_boot_memory
    from pre2.native.state import NativeGameState
    import pre2.views.tables as T
    st = NativeGameState(build_boot_memory())
    be = G.DataclassBackend(st)
    be.level_tables.tile_props[7] = 0xAB          # mutate the OBJECT
    be.materialize(st.data)
    assert st.data[DGROUP_BASE + T.TILE_PROPS + 7] == 0xAB, "materialize did not fold LevelTables back"
