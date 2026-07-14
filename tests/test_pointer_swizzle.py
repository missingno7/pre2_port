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


def test_current_hit_object_is_adopted_as_a_reference_in_the_shipped_model():
    """ADOPTION proof: the shipped model's `SceneryState.current_hit_object` (cyxx name) genuinely stores an
    offset-free reference — NOT a raw 16-bit offset — while the bridge swizzles ref<->offset at the byte
    boundary so the tick stays byte-exact. Writing a pool offset through rb/rw stores an ObjectRef; reading it
    back reproduces the exact offset."""
    from pre2.bridge.game_layout import DataclassBackend
    from pre2.game.ref import ObjectRef, RawRef
    from pre2.native.state import NativeGameState

    st = NativeGameState(bytearray(0x10000 + (0x1A0F << 4)))
    dcb = DataclassBackend(st, readonly_image=False)
    sc = dcb._objs["scenery_state"]
    assert isinstance(sc.current_hit_object, (ObjectRef, RawRef)), "field must hold a reference, not an int"

    st.backend = dcb
    dcb.ww(0x6BB1, 0x4FD0)                                    # write actors[0]'s offset via the byte path
    assert sc.current_hit_object == ObjectRef("actors", 0), "a pool offset must be stored as an ObjectRef"
    assert dcb.rw(0x6BB1) == 0x4FD0, "reading the ref back must reproduce the exact offset"
    dcb.ww(0x6BB1, 0x0000)                                    # the null sentinel
    assert isinstance(sc.current_hit_object, RawRef) and dcb.rw(0x6BB1) == 0


def test_all_five_pool_pointer_globals_are_stored_as_references():
    """Every pointer in _REF_FIELDS resolves to a dataclass field that genuinely holds an offset-free reference
    (ObjectRef/RawRef), not a raw int — the full pointer-globals family is adopted, byte-swizzled by the bridge."""
    from pre2.bridge.game_layout import _REF_FIELDS, DataclassBackend
    from pre2.game.ref import ObjectRef, RawRef
    from pre2.native.state import NativeGameState

    assert _REF_FIELDS == {"current_hit_object", "spawned_ptr", "cam_target_ptr", "target_a", "target_b"}
    st = NativeGameState(bytearray(0x10000 + (0x1A0F << 4)))
    dcb = DataclassBackend(st, readonly_image=False)
    found = 0
    for inst in dcb._objs.values():
        for name in _REF_FIELDS:
            if hasattr(inst, name) and name in getattr(inst, "__dataclass_fields__", {}):
                assert isinstance(getattr(inst, name), (ObjectRef, RawRef)), f"{name} must hold a reference"
                found += 1
    assert found == len(_REF_FIELDS), f"expected all {len(_REF_FIELDS)} ref fields, found {found}"


def test_arena_swizzle_def_ptr_points_to_a_source_entity_and_round_trips():
    """def_ptr (each object's type-definition pointer, cyxx monster_t.ref) points INTO the variable-stride entity
    ARENA, not a static pool. Prove the instance-aware arena swizzle on real corpus states: every live actor's
    non-null def_ptr lands on an arena record boundary -> an ArenaRef that round-trips to the exact offset and
    derefs to the source ArenaEntity the offset view reads. The adoption precondition for def_ptr, mirroring the
    pool-pointer proof."""
    from pre2.bridge.game_layout import DataclassBackend
    from pre2.game.ref import ArenaRef, RawRef
    from pre2.native.game_tick_demo import GameTickDemo, _inject
    from pre2.native.loop import native_gameplay_frame
    from pre2.native.state import NativeGameState

    demos = ["demo_pre2_full_gorilla_20260628_203423", "demo_pre2_20260704_235611"]
    seen_arena = 0
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
            starts = {start for start, _e in dcb._arena}
            for a in dcb.actors:
                # def_ptr is ADOPTED: it is stored as a ref (ArenaRef/RawRef), not a raw int
                assert isinstance(a.def_ptr, (ArenaRef, RawRef)), "def_ptr must hold a reference"
                off = dcb.arena_to_offset(a.def_ptr)                    # the offset it references in this state
                assert dcb.arena_from_offset(off) == a.def_ptr, "arena swizzle broke round-trip"
                if a.sprite == 0xFFFF or off in (0, 0xFFFF):
                    continue
                if off in starts:
                    assert isinstance(a.def_ptr, ArenaRef), f"{off:#06x} is an arena record but not an ArenaRef"
                    assert dcb._arena[a.def_ptr.index][0] == off        # derefs to the source entity's record
                    seen_arena += 1
    assert seen_arena > 0, "no live actor def_ptr ever resolved to an arena entity — swizzle unexercised"


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
    # def_ptr is excluded: it is an ArenaRef (a reference), not a plain int — proven separately by the arena test.
    fields = ("x", "y", "sprite", "xvel", "yvel", "anim_ptr", "state", "hp", "hits")
    base, count = POOL_REGIONS["actors"]
    for i in range(count):
        obj = deref(ObjectRef("actors", i), pools)      # offset-free resolution
        off_view = ObjectSlot(st, to_offset(ObjectRef("actors", i)))  # bridge swizzles the ref -> the DOS offset
        for f in fields:
            assert getattr(obj, f) == getattr(off_view, f), f"actors[{i}].{f} deref != offset view"
