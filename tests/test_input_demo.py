from __future__ import annotations

import json
from types import SimpleNamespace

from dos_re.cpu import CPUState
from dos_re.input_demo import InputDemoPlayback, dos_key_value


class DummyRuntime:
    def __init__(self) -> None:
        self.dos = SimpleNamespace(key_queue=[])
        self.scans: list[int] = []


def _deliver(rt: DummyRuntime, scancode: int) -> None:
    rt.scans.append(scancode & 0xFF)


def test_input_demo_playback_applies_events_once_at_recorded_boundaries(tmp_path):
    demo = tmp_path / "demo"
    demo.mkdir()
    (demo / "snapshot").mkdir()
    (demo / "input_demo.json").write_text(
        json.dumps(
            {
                "version": 1,
                "snapshot": "snapshot",
                "events": [
                    {"boundary": 0, "seq": 0, "kind": "scan", "value": 0x4D},
                    {"boundary": 2, "seq": 1, "kind": "dos_key", "value": 0x3920, "scancode": 0x39, "text": " "},
                    {"boundary": 2, "seq": 2, "kind": "scan", "value": 0xCD},
                ],
            }
        ),
        encoding="utf-8",
    )

    playback = InputDemoPlayback.load(demo)
    rt = DummyRuntime()

    assert playback.snapshot_path() == demo / "snapshot"
    assert playback.apply_to_runtime(0, rt, deliver=_deliver) == 1
    assert rt.scans == [0x4D]
    assert rt.dos.key_queue == []

    assert playback.apply_to_runtime(1, rt, deliver=_deliver) == 0
    assert playback.apply_to_runtime(2, rt, deliver=_deliver) == 2
    assert rt.scans == [0x4D, 0xCD]
    assert rt.dos.key_queue == [0x3920]

    assert playback.apply_to_runtime(99, rt, deliver=_deliver) == 0


def test_input_demo_playback_can_feed_reference_and_candidate_pair(tmp_path):
    demo = tmp_path / "demo"
    demo.mkdir()
    (demo / "input_demo.json").write_text(
        json.dumps(
            {
                "version": 1,
                "snapshot": "snapshot",
                "events": [
                    {"boundary": 1, "seq": 0, "kind": "scan", "value": 0x39},
                ],
            }
        ),
        encoding="utf-8",
    )

    playback = InputDemoPlayback.load(demo)
    ref = DummyRuntime()
    cand = DummyRuntime()

    assert playback.apply_to_runtimes(0, (ref, cand), deliver=_deliver) == 0
    assert playback.apply_to_runtimes(1, (ref, cand), deliver=_deliver) == 1
    assert ref.scans == cand.scans == [0x39]


def test_dos_key_value_matches_text_prompt_encoding():
    assert dos_key_value(0x39, "") == 0x3920
    assert dos_key_value(0x1C, "") == 0x1C0D
    assert dos_key_value(0x2C, "z") == 0x2C7A
    assert dos_key_value(0x3B, "") is None


def test_input_demo_suffix_uses_playback_cursor_not_boundary_filter(tmp_path):
    demo = tmp_path / "demo"
    demo.mkdir()
    (demo / "input_demo.json").write_text(
        json.dumps(
            {
                "version": 1,
                "snapshot": "snapshot",
                "end_boundary": 7,
                "metadata": {"video": "vga"},
                "events": [
                    {"boundary": 2, "seq": 0, "kind": "scan", "value": 0x4D},
                    {"boundary": 2, "seq": 1, "kind": "scan", "value": 0xCD},
                    {"boundary": 5, "seq": 2, "kind": "dos_key", "value": 0x3920, "scancode": 0x39, "text": " "},
                ],
            }
        ),
        encoding="utf-8",
    )
    playback = InputDemoPlayback.load(demo)
    rt = DummyRuntime()

    assert playback.apply_to_runtime(2, rt, deliver=_deliver) == 2
    suffix = playback.remaining_events_from_cursor(boundary=2)

    assert [event.to_json() for event in suffix] == [
        {"boundary": 3, "seq": 0, "kind": "dos_key", "value": 0x3920, "scancode": 0x39, "text": " "},
    ]


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
            instruction_count=123,
            s=CPUState(cs=0x1010, ip=0x1234, flags=0x0202),
            hook_names={},
        )
        self.dos = SimpleNamespace(
            video_mode=0,
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


def test_input_demo_write_suffix_writes_snapshot_and_rebased_manifest(tmp_path):
    demo = tmp_path / "demo"
    demo.mkdir()
    (demo / "input_demo.json").write_text(
        json.dumps(
            {
                "version": 1,
                "snapshot": "snapshot",
                "end_boundary": 10,
                "metadata": {"video": "vga", "sound": "adlib"},
                "events": [
                    {"boundary": 4, "seq": 0, "kind": "scan", "value": 0x4D},
                    {"boundary": 9, "seq": 1, "kind": "scan", "value": 0xCD},
                ],
            }
        ),
        encoding="utf-8",
    )
    playback = InputDemoPlayback.load(demo)
    playback.apply_to_runtime(4, DummyRuntime(), deliver=_deliver)

    out = playback.write_suffix(
        SnapshotRuntime(),
        root=tmp_path / "repros",
        name="verify_divergence",
        boundary=4,
        status="test suffix snapshot",
        metadata={"reason": "unit-test"},
    )

    manifest = json.loads((out / "input_demo.json").read_text(encoding="utf-8"))
    assert (out / "snapshot" / "state.json").exists()
    assert manifest["end_boundary"] == 6
    assert manifest["metadata"]["video"] == "vga"
    assert manifest["metadata"]["reason"] == "unit-test"
    assert manifest["metadata"]["source_boundary"] == 4
    assert manifest["events"] == [
        {"boundary": 5, "seq": 0, "kind": "scan", "value": 0xCD},
    ]
