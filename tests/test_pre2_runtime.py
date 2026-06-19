from pathlib import Path

from pre2.analysis import describe_exe, inventory_assets
from pre2.runtime import create_pre2_runtime

ROOT = Path(__file__).resolve().parents[1]


def test_pre2_asset_inventory_finds_original_files():
    inv = inventory_assets(ROOT / "assets")
    assert inv.exe.name.lower() == "pre2.exe"
    assert len(inv.sqz_files) >= 30
    assert len(inv.trk_files) >= 10


def test_pre2_exe_is_packed_mz_without_overlay():
    desc = describe_exe(ROOT / "assets" / "pre2.exe")
    assert desc["entry_cs"] == 0x0CA6
    assert desc["entry_ip"] == 0x000E
    assert desc["relocations"] == 0
    assert desc["overlay_size"] == 0


def test_pre2_vm_runs_to_inner_code_without_legacy_package():
    rt = create_pre2_runtime(ROOT / "assets" / "pre2.exe", game_root=ROOT / "assets")
    rt.cpu.trace_enabled = False
    rt.cpu.run(5_000)
    assert rt.cpu.s.cs in {0x1996, 0x1C34}
    assert any("pre2_bootstrap_lzexe" in name for name in rt.cpu.hook_names.values())
