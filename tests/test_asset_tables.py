"""Gate: the boot-constant gameplay tables are NATIVE immutable assets, byte-identical to the DOS spans.

P5 slice 1a (docs/pre2/native_dataclass_lift.md). ``pre2/native/asset_tables.py`` holds trig, sprite metrics,
score values and anim-frame descriptors as plain Python bytes, and ``views/tables.Tables`` reads them directly
instead of ``rb(0x6F90 + angle)`` into the historical image. Two things must stay true forever, and neither is
self-evident:

1. the literals equal the spans the DOS build held (else the game silently plays with wrong content);
2. those spans really are CONSTANT — which is NOT implied by "populated at boot" and NOT implied by "not routed
   to the object graph". Both heuristics are wrong: they would have frozen ``sprite_left_hw``, which the level
   loader rewrites (its region is interleaved -- even byte = the constant half-width, odd byte = loader-written).
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DGROUP_BASE = 0x1A0F << 4


def _spans():
    import pre2.views.tables as T
    import pre2.native.asset_tables as A
    return [("SONG_INDEX", A.SONG_INDEX, T.SONG_INDEX), ("COS", A.COS, T.COS), ("SIN", A.SIN, T.SIN),
            ("SPRITE_GEOM", A.SPRITE_GEOM, T.SPRITE_GEOM), ("SPEED_CURVE", A.SPEED_CURVE, T.SPEED_CURVE),
            ("SCORE_TABLE", A.SCORE_TABLE, T.SCORE_TABLE), ("SCORE_SPR_LUT", A.SCORE_SPR_LUT, T.SCORE_SPR_LUT),
            ("HURT_SFX", A.HURT_SFX, T.HURT_SFX_TABLE), ("ANIM_FRAME", A.ANIM_FRAME, T.ANIM_FRAME_TABLE)]


def test_native_asset_tables_are_byte_identical_to_the_dos_spans():
    """Each native literal == the boot image's span at that table's base. Pins the two so they cannot drift:
    a regenerated asset module or a changed base fails here rather than silently changing gameplay content."""
    from pre2.native.boot_data import build_boot_memory
    mem = build_boot_memory()
    for name, native, base in _spans():
        want = bytes(mem[DGROUP_BASE + base:DGROUP_BASE + base + len(native)])
        assert native == want, f"{name}: native asset differs from the DOS span at 0x{base:04X}"


def test_the_generated_asset_module_is_current():
    """The checked-in module must equal a fresh generation (same guard as tests/test_field_registry.py)."""
    import subprocess
    import sys
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "gen_asset_tables.py"), "--check"],
                       capture_output=True, text=True, cwd=str(ROOT))
    assert r.returncode == 0, f"pre2/native/asset_tables.py is stale — run gen_asset_tables.py\n{r.stdout}"


def test_the_migrated_tables_really_are_constant_across_a_real_level_load():
    """The claim that earns the migration: these spans survive a real cold boot + *.SQZ level load unchanged.

    This is the check that caught ``sprite_left_hw`` (loader-mutated, therefore deliberately NOT migrated). If a
    future asset is added to the generated module without being genuinely constant, this fails loudly here
    instead of producing a wrong lookup at runtime."""
    import pytest
    from pre2.native.boot_data import build_boot_memory
    from pre2.native.cold_boot import native_cold_boot
    if not (ROOT / "assets").exists():
        pytest.skip("game assets not present")
    mem = build_boot_memory()
    state = native_cold_boot(str(ROOT / "assets"), level=0)
    for name, native, base in _spans():
        boot = bytes(mem[DGROUP_BASE + base:DGROUP_BASE + base + len(native)])
        after = bytes(state.data[DGROUP_BASE + base:DGROUP_BASE + base + len(native)])
        assert after == boot, (f"{name} @0x{base:04X} was MUTATED by the loader — it is not a boot constant "
                               f"and must not be a native literal (see sprite_left_hw)")


def test_sprite_left_hw_is_correctly_excluded_as_loader_mutated():
    """Guards the negative result, so nobody 'completes' the set by adding it. The 0x752A region is INTERLEAVED:
    the even byte (id<<1) is the constant half-width; the odd byte is loader-written. Freezing the whole span
    would be a silent wrong answer -- exactly the failure this slice's method exists to prevent."""
    import pytest
    import pre2.native.asset_tables as A
    import pre2.views.tables as T
    from pre2.native.boot_data import build_boot_memory
    from pre2.native.cold_boot import native_cold_boot

    assert not hasattr(A, "SPRITE_LEFT_HW"), (
        "sprite_left_hw must NOT be a native literal: the loader rewrites its odd bytes")
    if not (ROOT / "assets").exists():
        pytest.skip("game assets not present")
    mem = build_boot_memory()
    state = native_cold_boot(str(ROOT / "assets"), level=0)
    b, ln = T.SPRITE_LEFT_HW, 922
    boot = bytes(mem[DGROUP_BASE + b:DGROUP_BASE + b + ln])
    after = bytes(state.data[DGROUP_BASE + b:DGROUP_BASE + b + ln])
    changed = [i for i in range(ln) if boot[i] != after[i]]
    assert changed, "sprite_left_hw no longer mutates — re-audit before migrating it"
    assert all(i % 2 for i in changed), "the interleave assumption broke: EVEN bytes changed too"
