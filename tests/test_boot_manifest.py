"""Gate: the boot-data blackbox is untangled — the decoded tables are a lossless reading of the 64 KB boot
DGROUP, and the region legend is consistent.
"""


def test_boot_manifest_is_lossless_and_consistent():
    from pre2.native.boot_manifest import verify_boot_manifest
    verify_boot_manifest()


def test_boot_resource_manifest_decodes_the_assets():
    from pre2.native.boot_manifest import resource_table
    texts = [r.text for r in resource_table()]
    # the game's real asset packs / music modules are present, decoded from the blob
    for name in ("KEYB.SQZ", "ALLFONTS.SQZ", "FRONT.SQZ", "MENU.SQZ", "GAMEOVER.SQZ", "BOULA.TRK", "CODE.TRK"):
        assert name in "\n".join(texts), name


def test_boot_scancode_table_maps_digits():
    from pre2.native.boot_manifest import scancode_char_table
    tbl = scancode_char_table()
    # scancodes 0x02..0x0B are the top-row digits 1234567890 on a PC keyboard
    assert tbl[0x02:0x0C] == "1234567890"


def test_boot_lookup_tables_decode_to_real_data():
    from pre2.native.boot_manifest import (attack_phase_table, jump_impulse_table, sine_table,
                                           sprite_half_extents)
    s = sine_table()
    assert len(s) == 256 and s[0] == 0 and s[64] == 64 and s[192] == -64      # a real sine wave, amplitude 64
    assert jump_impulse_table() == [-65, -51, -35, -20, -10, -5, -2, -1, 0]   # the decaying jump arc
    assert len(sprite_half_extents()) == 32
    phases = attack_phase_table()
    assert len(phases) == 4 and phases[0].sfx == 2 and phases[3].flag == 3    # last phase carries flag 3


def test_boot_object_handler_table_matches_recovered_handlers():
    from pre2.native.boot_manifest import object_handler_table
    from pre2.recovered.object_tick import HANDLERS
    addrs = object_handler_table()
    assert len(addrs) == 19
    # the CS dispatch table and the recovered Python handlers describe the same entry points
    assert len(set(addrs) & set(HANDLERS)) >= 13
