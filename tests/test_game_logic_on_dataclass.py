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


# PlayerGlobals fields not yet exposed BY NAME on any globals-cluster dataclass. Two remaining categories:
# (1) currently stored as raw bytes in a ByteBuffer/sparse buffer (no field name at all yet — needs a proper
#     dataclass field), (2) not routed at all (read via the level_data/image fallback). Shrinks as globals-
# cluster coverage grows; this test tracks the count so it can't silently grow back.
_KNOWN_GLOBALS_GAP = {
    "anim_ready", "attack_phase", "attack_v19", "attract_level", "burst_sprite", "burst_x", "burst_y",
    "cam_param_e", "cam_state", "cam_target_ptr", "cam_timer", "cmd_byte", "col_ring", "collected_linked",
    "cursor_latch_x", "cursor_latch_y", "cursor_x", "cursor_y", "dipping_tile", "dist_dir", "dist_x",
    "firefly_scratch_a", "firefly_scratch_b", "grid_dirty_token", "hit_debounce", "hit_detail", "hit_flag",
    "hit_pass_full", "idle_clock", "in_aux", "page_dirty", "quake_dist_hi", "quake_dist_lo", "script_cursor",
    "script_ptr", "spawn_count", "spawn_offset_ring", "spawned_ptr", "target_a", "target_b",
    "attract_mode", "boss_phase", "boss_x", "boss_y", "cam_left", "current_object", "display_page",
    "glider_tilt", "map_rows", "mode", "mode_copy", "sprite_bank_hi", "sprite_bank_lo",
}


def test_globals_field_name_gap_does_not_grow():
    """Tracks PlayerGlobals fields still missing BY NAME from the routed globals-cluster dataclasses. Must
    shrink (or stay flat) over time, never grow — a regression here means a newly-added global field wasn't
    given a matching name, undermining gap #3."""
    from pre2.game.model import Camera, Input, LevelState, Motion, PlayerState, Progress, Scroll
    dv = (ROOT / "pre2" / "views" / "dgroup_view.py").read_text(encoding="utf-8")
    pg_body = dv.split("class PlayerGlobals")[1].split("\nclass ")[0]
    import re
    all_globals = set(re.findall(r"^\s*([a-z][a-z0-9_]*)\s*=\s*_[US]", pg_body, re.M))
    covered = set()
    for dc in (Camera, Input, LevelState, Motion, PlayerState, Progress, Scroll):
        covered |= _dc_names(dc)
    gap = all_globals - covered
    assert gap <= _KNOWN_GLOBALS_GAP, f"new/unexpected globals gap (needs a name): {sorted(gap - _KNOWN_GLOBALS_GAP)}"
