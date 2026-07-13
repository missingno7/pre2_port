"""Gap #3 finding: the recovered game logic already reads state by NAME (via views), so it runs UNCHANGED on
the offset-free game dataclasses — provided the dataclass exposes the same field names the logic reads.

This reframes gap #3 (detach the offset layer from the game code) from 'rewrite the verified ASM transcription'
to 'field-name parity + plumb the dataclasses in'. Proven here on the RNG; the same holds for any structure
whose dataclass has field-name parity with its view.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_recovered_rng_logic_runs_byte_exact_on_the_offset_free_dataclass():
    from pre2.game.model import Rng
    from pre2.native.game_tick_demo import GameTickDemo
    from pre2.native.state import NativeGameState
    from pre2.recovered.combat_interaction import roll_bonus_sprite
    from pre2.views.dgroup_view import RngView

    demo = ROOT / "artifacts" / "demo_cold_20260712_172030" / "game_tick_demo.bin"
    if not demo.exists():
        import pytest
        pytest.skip("cold demo corpus not present")
    gtd = GameTickDemo.load(demo)
    st = NativeGameState(bytearray(gtd.seed))
    view = RngView(st)                                                   # offset-backed (lcg_a = _U8(0x2CEC))
    dc = Rng(view.lcg_a, view.lcg_b, view.lcg_c, view.lcg_d, view.ror)   # offset-FREE plain dataclass

    # the SAME recovered function, run on the view vs the dataclass — identical output and mutated state
    assert [roll_bonus_sprite(view) for _ in range(30)] == [roll_bonus_sprite(dc) for _ in range(30)]
    assert (view.lcg_a, view.lcg_b, view.lcg_c, view.lcg_d) == (dc.lcg_a, dc.lcg_b, dc.lcg_c, dc.lcg_d)
    # the dataclass genuinely has no offsets / view machinery
    assert type(dc).__mro__[1] is object


def test_a_real_composed_recovered_function_runs_identically_on_both_backends():
    """The key gap-#3 mechanism proof: a real, non-trivial recovered function (not a toy example) — one that
    rolls the RNG 4 times via RngView + spawns 4 effect bursts, returning a 37-entry write contract — produces
    IDENTICAL writes and an identical resulting DGROUP whether called through the offset-backed ByteBackend or
    the offset-free DataclassBackend, with ZERO code changes. This is because ``readers(mem)`` already routes
    through ``mem.backend`` (memory_adapter.readers), so the existing pure-function/write-contract architecture
    is ALREADY compatible with the object graph — no per-callsite rewiring is needed for the tick to run on it
    (proven end-to-end by verify_object_full.py); what remained was field-name parity, now done."""
    from pre2.bridge.game_layout import DataclassBackend
    from pre2.native.game_tick_demo import GameTickDemo
    from pre2.native.state import NativeGameState
    from pre2.recovered.object_spawn import boss_death_burst_94f3
    from pre2.views.memory_adapter import apply_ds, readers

    demo = ROOT / "artifacts" / "demo_cold_20260712_172030" / "game_tick_demo.bin"
    if not demo.exists():
        import pytest
        pytest.skip("cold demo corpus not present")
    gtd = GameTickDemo.load(demo)
    DGROUP_BASE = 0x1A0F << 4

    byte_st = NativeGameState(bytearray(gtd.seed))
    obj_st = NativeGameState(bytearray(gtd.seed))
    obj_st.backend = DataclassBackend(obj_st, readonly_image=True)

    w1 = boss_death_burst_94f3(*readers(byte_st))
    w2 = boss_death_burst_94f3(*readers(obj_st))
    assert w1 == w2 and len(w1) > 30

    apply_ds(byte_st, w1)
    apply_ds(obj_st, w2)
    obj_st.backend.materialize()
    assert byte_st.data[DGROUP_BASE:DGROUP_BASE + 0x10000] == obj_st.data[DGROUP_BASE:DGROUP_BASE + 0x10000]


def test_player_collision_runs_identically_on_both_backends():
    """A second, structurally different mechanism proof: player_collision.collision() uses PlayerView +
    PlayerGlobals + Tables (not the RNG-overlay pattern), returns a 3-tuple (ds_writes, map_writes, redraws),
    and is gated on branches (out-of-camera-range, airborne state) — exercised after real gameplay ticks so the
    player is in a non-trivial physical state. Same result: identical writes, identical DGROUP, both backends."""
    from pre2.bridge.game_layout import DataclassBackend
    from pre2.native.game_tick_demo import GameTickDemo, _inject
    from pre2.native.loop import native_gameplay_frame
    from pre2.native.state import NativeGameState
    from pre2.recovered.player_collision import collision
    from pre2.views.memory_adapter import apply_ds, readers

    demo = ROOT / "artifacts" / "demo_pre2_full_gorilla_20260628_203423" / "game_tick_demo.bin"
    if not demo.exists():
        import pytest
        pytest.skip("gorilla demo corpus not present")
    gtd = GameTickDemo.load(demo)
    DGROUP_BASE = 0x1A0F << 4

    byte_st = NativeGameState(bytearray(gtd.seed))
    obj_st = NativeGameState(bytearray(gtd.seed))
    obj_st.backend = DataclassBackend(obj_st, readonly_image=True)
    for i in range(30):
        idle = gtd.idle[i] if i < len(gtd.idle) else None
        _inject(byte_st, gtd.keys[i], idle); native_gameplay_frame(byte_st)
        _inject(obj_st, gtd.keys[i], idle); native_gameplay_frame(obj_st)

    def call_collision(state):
        rb, rw = readers(state)
        es_base = (rw(0x2DDA) << 4) & 0xFFFFF
        return collision(rb, rw, lambda o: state.data[(es_base + (o & 0xFFFF)) & 0xFFFFF])

    ds1, map1, redraws1 = call_collision(byte_st)
    ds2, map2, redraws2 = call_collision(obj_st)
    assert ds1 == ds2 and map1 == map2 and redraws1 == redraws2 and len(ds1) > 0

    apply_ds(byte_st, ds1)
    apply_ds(obj_st, ds2)
    obj_st.backend.materialize()
    assert byte_st.data[DGROUP_BASE:DGROUP_BASE + 0x10000] == obj_st.data[DGROUP_BASE:DGROUP_BASE + 0x10000]


def _view_fields(dv, cls):
    import re
    body = dv.split(f"class {cls}")[1].split("\nclass ")[0]
    return set(re.findall(r"^\s*([a-z][a-z0-9_]*)\s*=\s*_[US]", body, re.M))


def _dc_names(dc):
    return set(dc.__dataclass_fields__) | {n for n in dir(dc) if isinstance(getattr(dc, n, None), property)}


def test_record_dataclasses_have_full_field_name_parity_with_their_views():
    """The precondition for gap #3, per record structure: every named field the view exposes (incl. the
    inherited RenderSlot fields and the width-alias fields) exists on the dataclass — so ANY recovered
    function reading those names runs unchanged on the offset-free dataclass."""
    from pre2.game.model import Actor, Player, Rng
    dv = (ROOT / "pre2" / "views" / "dgroup_view.py").read_text(encoding="utf-8")
    render = _view_fields(dv, "RenderSlot")
    for view, dc in [("PlayerView", Player), ("ObjectSlot", Actor), ("RngView", Rng)]:
        need = _view_fields(dv, view) | (render if view != "RngView" else set())
        gap = need - _dc_names(dc)
        assert not gap, f"{view} fields missing on {dc.__name__}: {sorted(gap)}"


# PlayerGlobals fields not yet exposed BY NAME on any globals-cluster dataclass. boss_x/boss_y are the sole
# remainder: they physically alias target_records[0].x/.y (a DOS memory overlay — two names, one word), so
# they ARE real routed fields already, just reached through a different (still-real) name. Shrinks over time.
_KNOWN_GLOBALS_GAP = {"boss_x", "boss_y"}


def test_globals_field_name_gap_does_not_grow():
    """Tracks PlayerGlobals fields still missing BY NAME from the routed globals-cluster dataclasses. Must
    shrink (or stay flat) over time, never grow — a regression here means a newly-added global field wasn't
    given a matching name, undermining gap #3."""
    from pre2.game.model import (AttackState, AttractState, Boss, Camera, CameraScript, DifficultyMode,
                                 HitScratch, Input, LevelState, Motion, PlayerState, Progress, SceneryState,
                                 Scroll, SpawnCursor)
    dv = (ROOT / "pre2" / "views" / "dgroup_view.py").read_text(encoding="utf-8")
    pg_body = dv.split("class PlayerGlobals")[1].split("\nclass ")[0]
    import re
    all_globals = set(re.findall(r"^\s*([a-z][a-z0-9_]*)\s*=\s*_[US]", pg_body, re.M))
    covered = set()
    for dc in (Camera, Input, LevelState, Motion, PlayerState, Progress, Scroll, AttackState, HitScratch,
              SpawnCursor, CameraScript, SceneryState, AttractState, DifficultyMode, Boss):
        covered |= _dc_names(dc)
    gap = all_globals - covered
    assert gap <= _KNOWN_GLOBALS_GAP, f"new/unexpected globals gap (needs a name): {sorted(gap - _KNOWN_GLOBALS_GAP)}"
    assert gap == _KNOWN_GLOBALS_GAP, f"gap shrank further ({sorted(_KNOWN_GLOBALS_GAP - gap)}) — lower the ratchet"
