"""Name-keyed views + a shipped, offset-free object backend — the foundation of the native-dataclass lift
(docs/pre2/native_dataclass_lift.md).

Unlike the offset-keyed descriptors in ``dgroup_view.py`` (``airborne = _U8(<dgroup offset>)`` — the offset
baked into shipped code), a name-keyed field carries NO offset: it knows only its own attribute NAME (captured via
``__set_name__``) plus width/sign, and reads/writes through ``backend.read_field`` / ``write_field``. The
shipped :class:`NamedObjectBackend` resolves ``(view, field-name)`` straight to a ``pre2/game`` dataclass field
by name — no offsets anywhere — so it ships with the game while the offset↔name layout (and the byte image it
serialises) detaches into ``pre2/bridge``.

This module adds nothing to the shipped default yet (P1 is additive); it establishes the mechanism, proven
byte-exact on the RNG (tests/test_named_view.py).
"""
from __future__ import annotations


class _NamedField:
    """A width/sign-typed field addressed by its own attribute NAME (no offset)."""

    __slots__ = ("name", "width", "signed")

    def __init__(self, width: int, signed: bool):
        self.width = width
        self.signed = signed
        self.name = ""

    def __set_name__(self, owner, name: str) -> None:
        self.name = name

    def __get__(self, o, owner=None):
        if o is None:
            return self
        return o._backend.read_field(o, self.name, self.width, self.signed)

    def __set__(self, o, v: int) -> None:
        o._backend.write_field(o, self.name, self.width, v)


def _u8() -> _NamedField:
    return _NamedField(1, False)


def _u16() -> _NamedField:
    return _NamedField(2, False)


def _s8() -> _NamedField:
    return _NamedField(1, True)


def _s16() -> _NamedField:
    return _NamedField(2, True)


class NamedView:
    """A view whose fields are addressed by name (no DGROUP offsets). Bind it to a backend that implements
    ``read_field(view, name, width, signed)`` / ``write_field(view, name, width, value)``."""

    __slots__ = ("_backend",)

    def __init__(self, backend):
        self._backend = backend


class NamedObjectBackend:
    """The shipped, OFFSET-FREE gameplay backend: resolves ``(view, field-name)`` to a ``pre2/game`` dataclass
    field by NAME. Register one dataclass instance per view class; reads/writes are a plain ``getattr``/
    ``setattr`` — there is not a single offset in this path, so it ships with the game and needs none of the
    detachable bridge. (Contrast the bridge's ``DataclassBackend``, which maps by OFFSET and is verification-
    only.) Writes are masked to the field width so the stored value matches the byte-image representation."""

    __slots__ = ("_objs",)

    def __init__(self):
        self._objs = {}

    def register(self, view_cls, obj) -> "NamedObjectBackend":
        """Route every ``view_cls`` field access to ``obj`` (its matching ``pre2/game`` dataclass)."""
        self._objs[view_cls] = obj
        return self

    def object_for(self, view_cls):
        return self._objs[view_cls]

    def read_field(self, view, name: str, width: int, signed: bool) -> int:
        # mask to the field width FIRST (the dataclass may hold the value in signed or raw form), then
        # sign-extend — matching the proven DataclassBackend/_S16 read convention exactly.
        v = getattr(self._objs[type(view)], name) & ((1 << (8 * width)) - 1)
        if signed and v & (1 << (8 * width - 1)):
            v -= 1 << (8 * width)
        return v

    def write_field(self, view, name: str, width: int, v: int) -> None:
        setattr(self._objs[type(view)], name, v & ((1 << (8 * width)) - 1))


class RngNamedView(NamedView):
    """The RNG state as a NAME-keyed view (no offsets) — the exemplar for the native-dataclass lift. Same field
    names and ``roll``/``roll_ror`` wiring as ``dgroup_view.RngView``, but resolved against a ``pre2/game.Rng``
    dataclass through :class:`NamedObjectBackend` instead of DGROUP offsets."""

    __slots__ = ()

    lcg_a = _u8()
    lcg_b = _u8()
    lcg_c = _u8()
    lcg_d = _u16()
    ror = _u16()

    def roll(self) -> int:
        from pre2.recovered.prng import rng_lcg
        a, b, c, d, ret = rng_lcg(self.lcg_a, self.lcg_b, self.lcg_c, self.lcg_d)
        self.lcg_a, self.lcg_b, self.lcg_c, self.lcg_d = a, b, c, d
        return ret

    def roll_ror(self) -> int:
        from pre2.recovered.prng import rng_ror
        new = rng_ror(self.ror)
        self.ror = new
        return new


class PlayerNamedView(NamedView):
    """The player kinematics/render record as a NAME-keyed view (no offsets) — the scale exemplar (11 canonical
    fields, signed velocities) for the native-dataclass lift. Same field names as ``dgroup_view.PlayerView``'s
    canonical fields, resolved against a ``pre2/game.Player`` dataclass through a name-keyed backend. The pure
    width-alias fields (``flags``/``facing_lo``/``life``/``sprite_id``) are omitted here: they are DERIVED
    re-projections the Player dataclass exposes as properties, not stored fields."""

    __slots__ = ()

    x = _u16()
    y = _u16()
    sprite = _u16()
    xvel = _s16()
    motion_mode = _u8()
    facing = _s16()
    anim_b = _u8()
    anim_ptr = _u16()
    yvel = _s16()
    run_flag = _u8()
    death_state = _u8()
