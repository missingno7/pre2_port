from pathlib import Path

from pre2.analysis import describe_exe, inventory_assets
from pre2.runtime import create_pre2_runtime

ROOT = Path(__file__).resolve().parents[1]


def test_pre2_asset_inventory_finds_original_files():
    inv = inventory_assets(ROOT / "assets")
    assert inv.exe.name.lower() == "pre2.exe"
    assert len(inv.sqz_files) >= 30
    assert len(inv.trk_files) >= 10


def test_pre2_exe_is_relocatable_mz_without_overlay():
    # PRE2.EXE is a standard relocatable MZ (entry 0020:0008, 134 relocations, no
    # overlay) that self-unpacks at runtime into the game code at segment 1030.
    desc = describe_exe(ROOT / "assets" / "pre2.exe")
    assert desc["entry_cs"] == 0x0020
    assert desc["entry_ip"] == 0x0008
    assert desc["relocations"] == 0x86
    assert desc["overlay_size"] == 0


def test_pre2_vm_runs_to_inner_code():
    rt = create_pre2_runtime(ROOT / "assets" / "pre2.exe", game_root=ROOT / "assets")
    rt.cpu.trace_enabled = False
    rt.cpu.run(5_000)
    # the bootstrap has materialized the real PRE2 program code at segment 1030.
    assert (rt.cpu.s.cs & 0xFFFF) == 0x1030
    assert any("pre2_bootstrap_lzexe" in name for name in rt.cpu.hook_names.values())
