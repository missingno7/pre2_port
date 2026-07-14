"""P1 of the native-dataclass lift (docs/pre2/native_dataclass_lift.md): the shipped, OFFSET-FREE name-keyed
backend is byte-exact.

Proves the mechanism on the RNG: the recovered PRNG advance run through a NAME-keyed RngNamedView over a plain
Rng dataclass (NamedObjectBackend — no offsets, ships) produces the identical roll SEQUENCE, and the identical
final state, as the shipped OFFSET-keyed RngView over the byte image. The bridge serialiser (rng_from_image /
rng_to_image) is the detachable half used only to seed the dataclass and to memcmp the result.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _seed_image():
    from pre2.native.game_tick_demo import GameTickDemo
    demo = ROOT / "artifacts" / "demo_cold_20260712_172030" / "game_tick_demo.bin"
    if not demo.exists():
        import pytest
        pytest.skip("cold demo corpus not present")
    return bytearray(GameTickDemo.load(demo).seed)


def test_named_rng_view_matches_offset_rng_view_byte_exact():
    from pre2.bridge.game_layout import rng_from_image, rng_to_image
    from pre2.views.dgroup_view import ByteBackend, RngView
    from pre2.views.named_view import NamedObjectBackend, RngNamedView

    img = _seed_image()

    # OFFSET-keyed path (shipped today): RngView over the byte image.
    off_view = RngView(ByteBackend_wrap(img))

    # NAME-keyed path (the lift): RngNamedView over a plain Rng dataclass, seeded via the detachable bridge.
    rng = rng_from_image(img)
    named = RngNamedView(NamedObjectBackend().register(RngNamedView, rng))

    # interleave both generators; every roll must agree, step for step.
    for i in range(200):
        if i % 3 == 0:
            assert off_view.roll_ror() == named.roll_ror()
        else:
            assert off_view.roll() == named.roll()

    # and the final dataclass serialises back to exactly the bytes the offset path left in the image.
    ref = bytearray(img)                     # img was mutated in place by the offset path
    out = bytearray(img)
    rng_to_image(rng, out)                   # fold the dataclass back over a copy
    DS = 0x1A0F << 4
    for _f, off, w, _s in __import__("pre2.bridge.game_layout", fromlist=["RNG_LAYOUT"]).RNG_LAYOUT:
        for k in range(w):
            assert out[DS + off + k] == ref[DS + off + k], f"rng byte {off + k:#06x} diverged"


def test_production_rng_view_is_name_capable_byte_exact():
    """P2 enabler: the SHIPPED production RngView (dgroup_view.py — its descriptors still carry offsets) now
    ALSO resolves by NAME when bound to a NamedObjectBackend, so the recovered roll logic runs byte-exact over
    a pre2/game.Rng dataclass without any change to the view or the logic. The shipped ByteBackend path is
    unchanged (it has no read_field -> offsets, as proven by the whole corpus)."""
    from pre2.bridge.game_layout import rng_from_image
    from pre2.views.dgroup_view import RngView
    from pre2.views.named_view import NamedObjectBackend

    img = _seed_image()
    off_view = RngView(ByteBackend_wrap(img))                 # offset path over the image
    name_view = RngView(NamedObjectBackend().register(RngView, rng_from_image(img)))  # name path over a dataclass

    for i in range(200):
        if i % 3 == 0:
            assert off_view.roll_ror() == name_view.roll_ror()
        else:
            assert off_view.roll() == name_view.roll()


def test_production_player_view_is_name_capable_byte_exact():
    """P2 enabler, on the player: the SHIPPED PlayerView resolves every canonical field identically over the
    image (offset path) and over a pre2/game.Player dataclass (name path), across real post-tick states."""
    from pre2.bridge.game_layout import player_from_image
    from pre2.views.dgroup_view import PlayerView
    from pre2.views.named_view import NamedObjectBackend

    for img in _real_player_states():
        off_view = PlayerView(ByteBackend_wrap(img))
        name_view = PlayerView(NamedObjectBackend().register(PlayerView, player_from_image(img)))
        for f in _PLAYER_FIELDS:
            assert getattr(off_view, f) == getattr(name_view, f), f"production PlayerView.{f} diverged"


def test_globals_megaview_is_name_capable_byte_exact():
    """P2, the hard case: PlayerGlobals is a MEGA-view (100+ fields spread across ~16 cluster dataclasses —
    Camera / Motion / Input / CameraScript / BossScript / ...). Field-name routing (register_fields), resolved
    by OFFSET in the bridge so cluster-local name collisions can't mis-route, lets the whole mega-view resolve
    name-first. Proven: every routed globals field reads identically over the image (offset path) and over the
    live cluster dataclasses (name path), across real post-tick states."""
    from pre2.bridge.game_layout import globals_field_routing
    from pre2.views.dgroup_view import PlayerGlobals
    from pre2.views.named_view import NamedObjectBackend

    states = _real_player_states()
    total_checked = 0
    for img in states:
        _insts, routing = globals_field_routing(img)
        assert len(routing) > 80, f"expected the bulk of the globals mega-view routed, got {len(routing)}"
        off_view = PlayerGlobals(ByteBackend_wrap(img))
        name_view = PlayerGlobals(NamedObjectBackend().register_fields(routing))
        for name in routing:
            assert getattr(off_view, name) == getattr(name_view, name), f"globals mega-view .{name} diverged"
            total_checked += 1
    assert total_checked > 600      # 8 states x 90+ fields


def test_named_object_backend_has_no_offsets():
    """The shipped name-keyed path must contain no DGROUP offsets at all — resolution is getattr by name."""
    import inspect

    from pre2.views import named_view
    src = inspect.getsource(named_view)
    # no hex offset literals in the module body (0x… of 3+ digits would be a DGROUP address).
    import re
    offsets = [m.group(0) for m in re.finditer(r"0x[0-9A-Fa-f]{3,}", src)]
    assert not offsets, f"name-keyed shipped module leaked offset literals: {offsets}"


def ByteBackend_wrap(img):
    from pre2.views.dgroup_view import ByteBackend

    class _Mem:
        def __init__(self, data):
            self.data = data
    return ByteBackend(_Mem(img))


_PLAYER_FIELDS = ("x", "y", "sprite", "xvel", "motion_mode", "facing", "anim_b", "anim_ptr", "yvel",
                  "run_flag", "death_state")


def _real_player_states(n_states=8, stride=60):
    """Drive the real tick over a demo and snapshot the DGROUP at intervals — real, non-trivial player states."""
    from pre2.native.game_tick_demo import GameTickDemo, _inject
    from pre2.native.loop import native_gameplay_frame
    from pre2.native.state import NativeGameState
    demo = ROOT / "artifacts" / "demo_pre2_full_gorilla_20260628_203423" / "game_tick_demo.bin"
    if not demo.exists():
        import pytest
        pytest.skip("gorilla demo corpus not present")
    gtd = GameTickDemo.load(demo)
    st = NativeGameState(bytearray(gtd.seed))
    out = []
    for i in range(n_states * stride):
        _inject(st, gtd.keys[i], gtd.idle[i] if i < len(gtd.idle) else None)
        native_gameplay_frame(st)
        if i % stride == stride - 1:
            out.append(bytearray(st.data))
    return out


def test_one_named_player_view_is_byte_exact_over_both_backends():
    """The P2 linchpin: a SINGLE name-keyed view definition (PlayerNamedView) resolves BYTE-IDENTICALLY whether
    backed by the shipped offset-free NamedObjectBackend (over a pre2/game.Player dataclass) or the bridge's
    NamedImageBackend (over the byte image via the offset layout). Proven on real post-tick player states across
    the corpus, for reads AND writes. This is what makes P2 tractable: one view, two backends, the bridge
    proving they agree — the image path can keep working while the object path becomes the shipped default."""
    from pre2.bridge.game_layout import (NamedImageBackend, PLAYER_BASE, PLAYER_LAYOUT, player_from_image,
                                         player_to_image)
    from pre2.views.named_view import NamedObjectBackend, PlayerNamedView

    DS = 0x1A0F << 4
    states = _real_player_states()
    for img in states:
        obj = PlayerNamedView(NamedObjectBackend().register(PlayerNamedView, player_from_image(img)))
        via_img = PlayerNamedView(NamedImageBackend(img, PLAYER_BASE, PLAYER_LAYOUT))
        # reads agree, field for field
        for f in _PLAYER_FIELDS:
            assert getattr(obj, f) == getattr(via_img, f), f"read {f} diverged"

        # writes agree: apply the same deltas via each path, then compare the resulting player bytes
        player = player_from_image(img)
        obj_w = PlayerNamedView(NamedObjectBackend().register(PlayerNamedView, player))
        img_w = bytearray(img)
        via_img_w = PlayerNamedView(NamedImageBackend(img_w, PLAYER_BASE, PLAYER_LAYOUT))
        for f in _PLAYER_FIELDS:
            v = (getattr(obj_w, f) + 7) & 0x7FFF
            setattr(obj_w, f, v)
            setattr(via_img_w, f, v)
        folded = bytearray(img)
        player_to_image(player, folded)
        for _f, off, w, _s in PLAYER_LAYOUT:
            for k in range(w):
                a = folded[DS + PLAYER_BASE + off + k]
                b = img_w[DS + PLAYER_BASE + off + k]
                assert a == b, f"write {_f} byte {off + k:#06x} diverged: object {a:#04x} vs image {b:#04x}"


_OBJECTSLOT_FIELDS = ["x", "y", "sprite", "life", "def_ptr", "xvel", "yvel", "anim_ptr", "state", "hp", "hits"]


def _states_with_live_actors(want=6):
    """Snapshot DGROUP images at ticks where the 0x4FD0 object pool actually holds live records (sprite !=
    0xFFFF) — the gorilla boss demo keeps that pool empty, so use a normal level with enemies on screen."""
    from pre2.native.game_tick_demo import GameTickDemo, _inject
    from pre2.native.loop import native_gameplay_frame
    from pre2.native.state import NativeGameState
    from pre2.gaps import Pre2HybridGap
    demo = ROOT / "artifacts" / "demo_pre2_20260704_235611" / "game_tick_demo.bin"
    if not demo.exists():
        import pytest
        pytest.skip("level demo corpus not present")
    gtd = GameTickDemo.load(demo)
    st = NativeGameState(bytearray(gtd.seed))
    DS = 0x1A0F << 4
    out = []
    for i in range(min(gtd.n_ticks, 700)):
        _inject(st, gtd.keys[i], gtd.idle[i] if i < len(gtd.idle) else None)
        try:
            native_gameplay_frame(st)
        except Pre2HybridGap:
            break
        live = sum(1 for k in range(12)
                   if (st.data[DS + 0x4FD0 + k * 0x12 + 4] | (st.data[DS + 0x4FD0 + k * 0x12 + 5] << 8)) != 0xFFFF)
        if live >= 2:
            out.append(bytearray(st.data))
        if len(out) >= want:
            break
    return out


def test_one_named_array_view_is_byte_exact_over_both_backends():
    """The array analog of the linchpin: a SINGLE name-keyed ARRAY view (ObjectSlotNamedView), addressed by
    INDEX (``actors[i]`` — no ``base + i*stride`` offset), resolves BYTE-IDENTICALLY whether backed by the
    shipped offset-free NamedObjectBackend (over a list of pre2/game.Actor dataclasses) or the bridge's
    NamedImageBackend (one per slot base, over the byte image via ACTOR_LAYOUT). Proven on real post-tick object
    pools across the corpus, reads AND writes. This lifts the mechanism from scalars/mega-views to record
    ARRAYS — the pools (actors/projectiles/bursts) that are the bulk of per-frame object mutation."""
    from pre2.bridge.game_layout import (ACTOR_BASE, ACTOR_COUNT, ACTOR_LAYOUT, ACTOR_STRIDE, NamedImageBackend,
                                         _obj_from_image, _obj_to_image)
    from pre2.game.model import Actor
    from pre2.views.named_view import NamedObjectBackend, ObjectSlotNamedView

    DS = 0x1A0F << 4
    states = _states_with_live_actors()
    saw_live = 0
    for img in states:
        actors = [_obj_from_image(Actor, ACTOR_LAYOUT, img, ACTOR_BASE + i * ACTOR_STRIDE)
                  for i in range(ACTOR_COUNT)]
        nb = NamedObjectBackend().register_array(ObjectSlotNamedView, actors)
        for i in range(ACTOR_COUNT):
            obj = ObjectSlotNamedView(nb, i)                                             # index-keyed, offset-free
            via_img = ObjectSlotNamedView(NamedImageBackend(img, ACTOR_BASE + i * ACTOR_STRIDE, ACTOR_LAYOUT), i)
            for f in _OBJECTSLOT_FIELDS:
                assert getattr(obj, f) == getattr(via_img, f), f"slot {i} read {f} diverged"
            if via_img.sprite != 0xFFFF:
                saw_live += 1

        # writeback parity: same deltas via each path -> identical resulting slot bytes
        actors_w = [_obj_from_image(Actor, ACTOR_LAYOUT, img, ACTOR_BASE + i * ACTOR_STRIDE)
                    for i in range(ACTOR_COUNT)]
        nb_w = NamedObjectBackend().register_array(ObjectSlotNamedView, actors_w)
        img_w = bytearray(img)
        for i in range(ACTOR_COUNT):
            ow = ObjectSlotNamedView(nb_w, i)
            iw = ObjectSlotNamedView(NamedImageBackend(img_w, ACTOR_BASE + i * ACTOR_STRIDE, ACTOR_LAYOUT), i)
            for f in _OBJECTSLOT_FIELDS:
                v = (getattr(ow, f) + 3) & 0x7FFF
                setattr(ow, f, v)
                setattr(iw, f, v)
        # fold the mutated object list back and compare the whole actor-pool region byte-for-byte
        folded = bytearray(img)
        for i in range(ACTOR_COUNT):
            _obj_to_image(actors_w[i], ACTOR_LAYOUT, folded, ACTOR_BASE + i * ACTOR_STRIDE)
        lo = DS + ACTOR_BASE
        hi = DS + ACTOR_BASE + ACTOR_COUNT * ACTOR_STRIDE
        assert folded[lo:hi] == img_w[lo:hi], "array writeback diverged between object and image backends"
    assert saw_live > 0, "no live actor slots in the corpus sample — the proof never exercised a real object"
