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
