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


def test_rng_dataclass_has_field_name_parity_with_the_view():
    """The precondition for gap #3: every named field the RNG view exposes exists on the Rng dataclass."""
    import re

    from pre2.game.model import Rng
    dv = (ROOT / "pre2" / "views" / "dgroup_view.py").read_text(encoding="utf-8")
    view_body = dv.split("class RngView")[1].split("\nclass ")[0]
    view_fields = set(re.findall(r"^\s*([a-z][a-z0-9_]*)\s*=\s*_[US]", view_body, re.M))
    dc_fields = set(Rng.__dataclass_fields__)
    assert view_fields <= dc_fields, f"RNG view fields missing on the dataclass: {view_fields - dc_fields}"
