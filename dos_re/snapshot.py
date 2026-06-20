from __future__ import annotations

import json
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from .cpu import HaltExecution, UnsupportedInstruction
from .runtime import Runtime


def parse_addr(text: str) -> tuple[int, int]:
    cs, ip = text.split(":", 1)
    return int(cs, 16) & 0xFFFF, int(ip, 16) & 0xFFFF


def run_until(
    rt: Runtime,
    *,
    max_steps: int,
    stop_at: tuple[int, int] | None = None,
    trace_tail: int = 0,
) -> tuple[str, int, list[str]]:
    """Run the interpreter and optionally keep only the last N trace lines."""
    tail: deque[str] = deque(maxlen=trace_tail)
    rt.cpu.trace_enabled = trace_tail > 0
    steps = 0
    try:
        for steps in range(1, max_steps + 1):
            if stop_at is not None and rt.cpu.addr() == stop_at:
                return f"reached {stop_at[0]:04X}:{stop_at[1]:04X}", steps - 1, list(tail)
            rt.cpu.step()
            if rt.cpu.trace:
                tail.extend(rt.cpu.trace)
                rt.cpu.trace.clear()
        return "stopped after max steps", steps, list(tail)
    except HaltExecution:
        return "program halted", steps, list(tail)
    except UnsupportedInstruction as e:
        return f"unsupported instruction: {e}", steps, list(tail)
    except Exception as e:  # keep snapshots useful even during emulator bring-up
        return f"exception: {type(e).__name__}: {e}", steps, list(tail)


def write_snapshot(rt: Runtime, out_dir: str | Path, *, status: str, steps: int, trace_tail: Iterable[str] = ()) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "memory_1mb.bin").write_bytes(bytes(rt.program.memory.data))
    (out / "trace_tail.txt").write_text("\n".join(trace_tail) + ("\n" if trace_tail else ""), encoding="utf-8")
    meta = {
        "status": status,
        "steps": steps,
        "cpu": asdict(rt.cpu.s),
        "cpu_snapshot": rt.cpu.s.snapshot(),
        "program": {
            "path": str(rt.program.exe.path),
            "psp_segment": rt.program.psp_segment,
            "load_segment": rt.program.load_segment,
            "entry_cs": rt.program.entry_cs,
            "entry_ip": rt.program.entry_ip,
            "initial_ss": rt.program.initial_ss,
            "initial_sp": rt.program.initial_sp,
            "load_module_size": len(rt.program.exe.load_module),
            "overlay_size": len(rt.program.overlay),
        },
        "dos": {
            "video_mode": rt.dos.video_mode,
            "video_page": rt.dos.video_page,
            "text_mode_active": rt.dos.text_mode_active,
            "cursor_row": rt.dos.cursor_row,
            "cursor_col": rt.dos.cursor_col,
            "ticks": rt.dos.ticks,
            "vga_status_reads": rt.dos.vga_status_reads,
            "vga_palette": [list(rgb) for rgb in getattr(rt.dos, "vga_palette", [])],
            "dac_write_index": getattr(rt.dos, "_dac_write_index", 0),
            "dac_read_index": getattr(rt.dos, "_dac_read_index", 0),
            "dac_component": getattr(rt.dos, "_dac_component", 0),
            "dac_latch": list(getattr(rt.dos, "_dac_latch", [])),
            "pit_channel2_access": rt.dos._pit_channel2_access,
            "pit_channel2_latch": rt.dos._pit_channel2_latch,
            "pit_channel2_write_low": rt.dos._pit_channel2_write_low,
            "pit_channel2_reload": rt.dos.pit_channel2_reload,
            "pit_channel0_access": getattr(rt.dos, "_pit_channel0_access", 3),
            "pit_channel0_latch": getattr(rt.dos, "_pit_channel0_latch", 0),
            "pit_channel0_write_low": getattr(rt.dos, "_pit_channel0_write_low", True),
            "pit_channel0_reload": getattr(rt.dos, "pit_channel0_reload", 0),
            "speaker_control": rt.dos.speaker_control,
            "opl_selected_register": rt.dos.opl_selected_register,
            "opl_status": rt.dos.opl_status,
            "opl_registers": {f"{reg:02X}": value for reg, value in sorted(rt.dos.opl_registers.items())},
            "ega_planar": rt.program.memory.ega_planar,
            "ega_map_mask": rt.program.memory.ega_map_mask,
            "ega_read_plane": rt.program.memory.ega_read_plane,
            "ega_data_rotate": getattr(rt.program.memory, "ega_data_rotate", 0),
            "ega_logical_op": getattr(rt.program.memory, "ega_logical_op", 0),
            "ega_write_mode": getattr(rt.program.memory, "ega_write_mode", 0),
            "ega_latches": list(getattr(rt.program.memory, "ega_latches", [0, 0, 0, 0])),
            "ega_display_start": rt.program.memory.ega_display_start,
            "next_alloc_segment": rt.dos.next_alloc_segment,
            "allocation_limit_segment": rt.dos.allocation_limit_segment,
            "allocations": {f"{seg:04X}": size for seg, size in sorted(rt.dos.allocations.items())},
            "open_files": {
                str(handle): {"path": str(f.path), "pos": f.pos, "size": len(f.data)}
                for handle, f in rt.dos.files.items()
            },
            "stdout_tail": "".join(rt.dos.stdout)[-4096:],
            "port_log_tail": rt.dos.port_log[-128:],
        },
        "hooks": {
            f"{cs:04X}:{ip:04X}": name for (cs, ip), name in sorted(rt.cpu.hook_names.items())
        },
    }
    # The emulated Sound Blaster / DMA programming is part of the machine state:
    # persist it so a save taken mid-playback resumes streaming (the front-end
    # re-attaches the SB and applies this via enable_sound_blaster).
    sound_blaster = getattr(rt.dos, "sound_blaster", None)
    if sound_blaster is not None:
        meta["dos"]["sound_blaster"] = sound_blaster.snapshot_state()
    (out / "state.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def load_snapshot(exe_path: str | Path, snapshot_dir: str | Path, *, game_root: str | Path | None = None) -> Runtime:
    """Create a Runtime from an existing snapshot directory.

    This is intentionally a developer/reverse-engineering helper: it restores
    CPU state, full 1MB memory, and simple DOS bookkeeping so investigation can
    continue from a known checkpoint instead of replaying the whole bootstrap.
    """
    from .cpu import CPUState
    from .dos import FileHandle
    from .runtime import create_runtime

    snap = Path(snapshot_dir)
    meta = json.loads((snap / "state.json").read_text(encoding="utf-8"))
    rt = create_runtime(exe_path, game_root=game_root)
    rt.program.memory.data[:] = (snap / "memory_1mb.bin").read_bytes()
    rt.cpu.mem = rt.program.memory
    rt.cpu.s = CPUState(**meta["cpu"])

    dos_meta = meta.get("dos", {})
    rt.dos.video_mode = dos_meta.get("video_mode", rt.dos.video_mode)
    rt.dos.video_page = dos_meta.get("video_page", rt.dos.video_page)
    if "text_mode_active" in dos_meta:
        rt.dos.text_mode_active = dos_meta["text_mode_active"]
    else:
        rt.dos.text_mode_active = False
    rt.dos.cursor_row = dos_meta.get("cursor_row", rt.dos.cursor_row)
    rt.dos.cursor_col = dos_meta.get("cursor_col", rt.dos.cursor_col)
    rt.dos.ticks = dos_meta.get("ticks", rt.dos.ticks)
    rt.dos.vga_status_reads = dos_meta.get("vga_status_reads", rt.dos.vga_status_reads)
    if "vga_palette" in dos_meta:
        rt.dos.vga_palette = [tuple(map(int, rgb)) for rgb in dos_meta["vga_palette"]]
    rt.dos._dac_write_index = dos_meta.get("dac_write_index", rt.dos._dac_write_index)
    rt.dos._dac_read_index = dos_meta.get("dac_read_index", rt.dos._dac_read_index)
    rt.dos._dac_component = dos_meta.get("dac_component", rt.dos._dac_component)
    rt.dos._dac_latch = list(dos_meta.get("dac_latch", rt.dos._dac_latch))
    rt.dos._pit_channel2_access = dos_meta.get("pit_channel2_access", rt.dos._pit_channel2_access)
    rt.dos._pit_channel2_latch = dos_meta.get("pit_channel2_latch", rt.dos._pit_channel2_latch)
    rt.dos._pit_channel2_write_low = dos_meta.get("pit_channel2_write_low", rt.dos._pit_channel2_write_low)
    rt.dos.pit_channel2_reload = dos_meta.get("pit_channel2_reload", rt.dos.pit_channel2_reload)
    rt.dos._pit_channel0_access = dos_meta.get("pit_channel0_access", rt.dos._pit_channel0_access)
    rt.dos._pit_channel0_latch = dos_meta.get("pit_channel0_latch", rt.dos._pit_channel0_latch)
    rt.dos._pit_channel0_write_low = dos_meta.get("pit_channel0_write_low", rt.dos._pit_channel0_write_low)
    rt.dos.pit_channel0_reload = dos_meta.get("pit_channel0_reload", rt.dos.pit_channel0_reload)
    rt.dos.speaker_control = dos_meta.get("speaker_control", rt.dos.speaker_control)
    rt.dos.opl_selected_register = dos_meta.get("opl_selected_register", rt.dos.opl_selected_register)
    rt.dos.opl_status = dos_meta.get("opl_status", rt.dos.opl_status)
    rt.dos.opl_registers = {int(reg, 16): int(value) for reg, value in dos_meta.get("opl_registers", {}).items()}
    if "pit_channel2_reload" not in dos_meta and "port_log_tail" in dos_meta:
        _restore_speaker_from_port_log_tail(rt, dos_meta.get("port_log_tail", ()))
    rt.program.memory.ega_planar = dos_meta.get("ega_planar", rt.program.memory.ega_planar)
    rt.program.memory.ega_map_mask = dos_meta.get("ega_map_mask", rt.program.memory.ega_map_mask)
    rt.program.memory.ega_read_plane = dos_meta.get("ega_read_plane", rt.program.memory.ega_read_plane)
    rt.program.memory.ega_data_rotate = dos_meta.get("ega_data_rotate", rt.program.memory.ega_data_rotate)
    rt.program.memory.ega_logical_op = dos_meta.get("ega_logical_op", rt.program.memory.ega_logical_op)
    rt.program.memory.ega_write_mode = dos_meta.get("ega_write_mode", rt.program.memory.ega_write_mode)
    rt.program.memory.ega_latches = list(dos_meta.get("ega_latches", rt.program.memory.ega_latches))
    rt.program.memory.ega_display_start = dos_meta.get("ega_display_start", rt.program.memory.ega_display_start)
    rt.dos.next_alloc_segment = dos_meta.get("next_alloc_segment", rt.dos.next_alloc_segment)
    rt.dos.allocation_limit_segment = dos_meta.get("allocation_limit_segment", rt.dos.allocation_limit_segment)
    rt.dos.allocations = {int(seg, 16): int(size) for seg, size in dos_meta.get("allocations", {}).items()}
    rt.dos.files.clear()
    for handle_text, file_meta in dos_meta.get("open_files", {}).items():
        path = Path(file_meta["path"])
        if not path.is_absolute():
            path = Path(path)
        if not path.exists():
            path = rt.dos.resolve_game_path(Path(file_meta["path"]).name)
        fh = FileHandle(path, bytearray(path.read_bytes()), pos=int(file_meta.get("pos", 0)))
        rt.dos.files[int(handle_text)] = fh
    if rt.dos.files:
        rt.dos.next_handle = max(rt.dos.files) + 1
    # Stash any persisted Sound Blaster state for the front-end to apply when it
    # attaches the SB (enable_sound_blaster); load_snapshot itself stays frontend-
    # agnostic and does not create audio hardware.
    rt.dos.sound_blaster_snapshot = dos_meta.get("sound_blaster")
    return rt


def _restore_speaker_from_port_log_tail(rt: Runtime, port_log_tail) -> None:
    """Best-effort PC-speaker state recovery for older snapshots.

    Pre-sound-state snapshots only stored the last few OUT instructions.  Replaying
    the speaker-related writes reconstructs the PIT channel-2 reload and port 61h
    gate when the tail contains the most recent tone setup, which is exactly the
    common F12-in-the-menu case.  The replay updates DOS hardware state only; it
    deliberately does not call a frontend speaker callback or append duplicate log
    entries.
    """
    saved_callback = rt.dos.speaker_callback
    rt.dos.speaker_callback = None
    try:
        for entry in port_log_tail or ():
            if not isinstance(entry, (list, tuple)) or len(entry) != 4:
                continue
            direction, port, value, bits = entry
            if direction != "out":
                continue
            port = int(port) & 0xFFFF
            if port not in (0x42, 0x43, 0x61):
                continue
            rt.dos._track_pc_speaker(port, int(value), int(bits))
    finally:
        rt.dos.speaker_callback = saved_callback
