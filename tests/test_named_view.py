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
