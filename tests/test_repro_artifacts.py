from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

from dos_re.cpu import CPUState
from dos_re.repro_artifacts import safe_artifact_part, write_runtime_repro_snapshot


class SnapshotRuntime:
    def __init__(self) -> None:
        self.program = SimpleNamespace(
            memory=SimpleNamespace(
                data=bytearray(1024 * 1024),
                size=1024 * 1024,
                ega_planar=False,
                ega_map_mask=0,
                ega_read_plane=0,
                ega_display_start=0,
            ),
            exe=SimpleNamespace(path="PRE2"),
            psp_segment=0,
            load_segment=0,
            entry_cs=0,
            entry_ip=0,
            initial_ss=0,
            initial_sp=0,
            overlay=b"",
        )
        self.program.exe.load_module = b""
        self.cpu = SimpleNamespace(
            instruction_count=321,
            s=CPUState(cs=0x1996, ip=0x07AC, flags=0x0202),
            hook_names={},
            addr=lambda: (0x1996, 0x07AC),
        )
        self.dos = SimpleNamespace(
            video_mode=2,
            video_page=0,
            text_mode_active=False,
            cursor_row=0,
            cursor_col=0,
            ticks=0,
            vga_status_reads=0,
            _pit_channel2_access=0,
            _pit_channel2_latch=0,
            _pit_channel2_write_low=None,
            pit_channel2_reload=0,
            speaker_control=0,
            opl_selected_register=0,
            opl_status=0,
            opl_registers={},
            next_alloc_segment=0,
            allocation_limit_segment=0,
            allocations={},
            files={},
            stdout=[],
            port_log=[],
        )


def test_safe_artifact_part_normalizes_paths_and_addresses():
    assert safe_artifact_part("crash sqz 1996:07AC/ValueError") == "crash_sqz_1996_07AC_ValueError"


def test_write_runtime_repro_snapshot_writes_loadable_snapshot_and_manifest(tmp_path):
    rt = SnapshotRuntime()
    out = write_runtime_repro_snapshot(
        rt,
        root=tmp_path,
        name="crash sqz ValueError",
        status="unit-test crash",
        metadata={"exception_type": "ValueError", "replay_hint": "python scripts/play.py --snapshot <this-directory>"},
        timestamp=datetime(2026, 6, 16, 13, 9, 0),
    )

    assert out.name == "crash_sqz_ValueError_20260616_130900"
    assert (out / "memory_1mb.bin").exists()
    assert (out / "state.json").exists()
    manifest = json.loads((out / "repro.json").read_text(encoding="utf-8"))
    assert manifest["kind"] == "runtime_snapshot"
    assert manifest["snapshot"] == "."
    assert manifest["cpu_addr"] == "1996:07AC"
    assert manifest["steps"] == 321
    assert manifest["metadata"]["exception_type"] == "ValueError"
