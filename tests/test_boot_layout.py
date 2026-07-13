"""Gate: the readable boot constants regenerate the original DOS layout byte-exact (the inversion — constants
are the source of truth, the DOS blob is generated from them for verification only).
"""


def test_constants_regenerate_the_dos_boot_layout():
    from pre2.bridge.boot_layout import verify_boot_layout
    verify_boot_layout()


def test_boot_tables_are_plain_readable_constants():
    from pre2.native import boot_tables as T
    assert len(T.SINE_TABLE) == 256 and T.SINE_TABLE[64] == 64
    assert T.JUMP_IMPULSE == [-65, -51, -35, -20, -10, -5, -2, -1, 0]
    assert any(t.endswith(".SQZ") for _, t in T.RESOURCE_RECORDS)
    # the constants module knows nothing of offsets/layout — that lives in the bridge
    import pre2.native.boot_tables as m
    assert not hasattr(m, "DS_BASE") and not hasattr(m, "generate_boot_dgroup")


def test_residual_is_fully_drained():
    from pre2.bridge.boot_layout import constant_coverage
    covered, residual = constant_coverage()
    assert covered > 20000              # the whole boot image is now named constants + PNG assets
    assert residual == 0                # nothing left in the opaque blob -> _DGROUP_Z is redundant
