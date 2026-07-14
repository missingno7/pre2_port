"""Pointer-swizzle linchpin proof (docs/pre2/pointer_swizzle_design.md, step 1).

Proves the mechanism byte-exact BEFORE any tick rewiring — the same way the name-keyed views were proven with
'one view, two backends' before adoption:
  1. round-trip identity: to_offset(from_offset(v)) == v for EVERY 16-bit value (the invariant that keeps a
     serialised image byte-identical to the DOS original);
  2. classification: pool-boundary offsets become the right ObjectRef(pool, index); everything else -> RawRef;
  3. deref parity: on real corpus states, deref(ObjectRef('actors', i)) resolves to the SAME live record as the
     offset-keyed ObjectSlot at that pool base + i*stride.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_round_trip_identity_over_all_16bit_values():
    """to_offset(from_offset(v)) == v for every v — the byte-exact invariant, exhaustively."""
    from pre2.bridge.pointer_layout import from_offset, to_offset
    for v in range(0x10000):
        assert to_offset(from_offset(v)) == v, f"swizzle broke round-trip at {v:#06x}"


def test_pool_offsets_classify_to_the_right_objectref():
    from pre2.bridge.pointer_layout import POOL_REGIONS, from_offset
    from pre2.game.ref import ObjectRef, RawRef
    STRIDE = 0x12
    for name, (base, count) in POOL_REGIONS.items():
        for i in range(count):
            ref = from_offset(base + i * STRIDE)
            assert ref == ObjectRef(name, i), f"{base + i*STRIDE:#06x} -> {ref!r}, want {name}[{i}]"
        # one byte past the last record is NOT this pool (disjoint, half-open region)
        assert not isinstance(from_offset(base + count * STRIDE), ObjectRef) or \
            from_offset(base + count * STRIDE) != ObjectRef(name, count)
        # a mid-record (unaligned) offset inside the region is NOT a clean pool ref -> RawRef
        if count and STRIDE > 1:
            assert isinstance(from_offset(base + 1), RawRef)


def test_sentinels_and_unclassified_are_raw_and_null():
    from pre2.bridge.pointer_layout import from_offset
    from pre2.game.ref import RawRef, is_null
    for sent in (0x0000, 0xFFFF):
        r = from_offset(sent)
        assert isinstance(r, RawRef) and is_null(r), f"{sent:#06x} should be a null RawRef"
    # an offset in no pool region (e.g. the globals area) -> opaque RawRef, round-tripping exactly
    assert from_offset(0x6BB1) == RawRef(0x6BB1)


def _a_real_state():
    from pre2.native.game_tick_demo import GameTickDemo, _inject
    from pre2.native.loop import native_gameplay_frame
    from pre2.native.state import NativeGameState
    demo = ROOT / "artifacts" / "demo_pre2_full_gorilla_20260628_203423" / "game_tick_demo.bin"
    if not demo.exists():
        import pytest
        pytest.skip("gorilla demo corpus not present")
    gtd = GameTickDemo.load(demo)
    st = NativeGameState(bytearray(gtd.seed))
    for i in range(120):
        _inject(st, gtd.keys[i], gtd.idle[i] if i < len(gtd.idle) else None)
        native_gameplay_frame(st)
    return st


def test_swizzle_handles_the_real_pointer_values_the_game_stores():
    """Adoption precondition: over real corpus states, the object-pool pointer GLOBALS (current_object,
    spawned_ptr, the camera-target ptrs) hold values the swizzle classifies + round-trips exactly, and every
    non-null one that lands in a pool derefs to the live record the offset view reads. Proves the swizzle works
    on the ACTUAL values the tick produces — not just synthetic refs — so these fields can become ObjectRefs."""
    from pre2.bridge.game_layout import DataclassBackend
    from pre2.bridge.pointer_layout import POOL_REGIONS, from_offset, to_offset
    from pre2.game.ref import ObjectRef, RawRef, deref, is_null
    from pre2.native.game_tick_demo import GameTickDemo, _inject
    from pre2.native.loop import native_gameplay_frame
    from pre2.native.state import NativeGameState
    from pre2.views.dgroup_view import ObjectSlot

    DS = 0x1A0F << 4
    PTR_FIELDS = {"current_object": 0x6BB1, "spawned_ptr": 0xA33E, "cam_target_ptr": 0xA421,
                  "target_a": 0xA423, "target_b": 0xA425}
    demos = ["demo_pre2_full_gorilla_20260628_203423", "demo_pre2_20260704_235611"]

    seen_objectref = 0
    seen_total = 0
    for d in demos:
        demo = ROOT / "artifacts" / d / "game_tick_demo.bin"
        if not demo.exists():
            continue
        gtd = GameTickDemo.load(demo)
        st = NativeGameState(bytearray(gtd.seed))
        for i in range(min(gtd.n_ticks, 400)):
            _inject(st, gtd.keys[i], gtd.idle[i] if i < len(gtd.idle) else None)
            try:
                native_gameplay_frame(st)
            except Exception as e:  # noqa: BLE001
                if type(e).__name__.startswith("Pre2"):
                    break
                raise
            if i % 20:
                continue
            dcb = DataclassBackend(st, readonly_image=False)
            pools = {n: (dcb._objs[n] if isinstance(dcb._objs.get(n), list) else [dcb._objs.get(n)])
                     for n in POOL_REGIONS}
            for off in PTR_FIELDS.values():
                v = st.data[DS + off] | (st.data[DS + off + 1] << 8)
                ref = from_offset(v)
                assert to_offset(ref) == v, f"real ptr {v:#06x} broke round-trip"
                seen_total += 1
                if isinstance(ref, ObjectRef):
                    seen_objectref += 1
                    rec = deref(ref, pools)
                    off_view = ObjectSlot(st, v)
                    for f in ("x", "y", "sprite"):
                        assert getattr(rec, f) == getattr(off_view, f), f"{ref} deref != offset view .{f}"
                else:
                    assert isinstance(ref, RawRef)  # null sentinel or points outside the pools
    assert seen_total > 0, "no corpus states sampled"
    assert seen_objectref > 0, "no real pointer ever resolved to a pool ObjectRef — swizzle unexercised on real data"


def test_deref_parity_objectref_resolves_to_the_same_record_as_the_offset_view():
    """A swizzled ObjectRef, dereferenced offset-free against the live pools, is the SAME record the offset-keyed
    ObjectSlot reads at the DOS offset — so pointer following can move to references with zero behaviour change."""
    from pre2.bridge.game_layout import DataclassBackend
    from pre2.bridge.pointer_layout import POOL_REGIONS, to_offset
    from pre2.game.ref import ObjectRef, deref
    from pre2.views.dgroup_view import ObjectSlot

    st = _a_real_state()
    dcb = DataclassBackend(st, readonly_image=False)
    pools = {"actors": dcb.actors}                      # the pool routed to live dataclasses
    fields = ("x", "y", "sprite", "def_ptr", "xvel", "yvel", "anim_ptr", "state", "hp", "hits")
    base, count = POOL_REGIONS["actors"]
    for i in range(count):
        obj = deref(ObjectRef("actors", i), pools)      # offset-free resolution
        off_view = ObjectSlot(st, to_offset(ObjectRef("actors", i)))  # bridge swizzles the ref -> the DOS offset
        for f in fields:
            assert getattr(obj, f) == getattr(off_view, f), f"actors[{i}].{f} deref != offset view"
