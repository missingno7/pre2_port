"""Helpers for reproducible crash/divergence artifacts.

These helpers intentionally live in ``dos_re`` because they are generic runtime
forensics: write a snapshot plus a small manifest explaining why it was captured.
Game-specific code decides when to call them and what metadata to attach.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping, Any

from .cpu import CPU8086, CPUState
from .dos import DOSMachine, FileHandle
from .memory import Memory
from .runtime import Runtime
from .snapshot import write_snapshot


def safe_artifact_part(text: str) -> str:
    """Return a filesystem-friendly artifact name component."""
    out = []
    for ch in str(text):
        if ch.isalnum() or ch in ("-", "_"):
            out.append(ch)
        elif ch in (" ", ":", "/", "\\", "."):
            out.append("_")
    cleaned = "".join(out).strip("_")
    return cleaned or "artifact"


def clone_runtime_state(src: Runtime) -> Runtime:
    """Return a detached runtime clone suitable for later repro snapshots.

    This intentionally clones VM/DOS state, not frontend callbacks or verifier
    progress hooks.  It is used when a verifier must preserve the state *before*
    a candidate hook/frame mutates the live runtime.
    """
    mem = Memory(0)
    mem.data = src.program.memory.data.copy()
    mem.size = src.program.memory.size
    mem.ega_planar = src.program.memory.ega_planar
    mem.ega_map_mask = src.program.memory.ega_map_mask
    mem.ega_read_plane = src.program.memory.ega_read_plane
    mem.ega_data_rotate = getattr(src.program.memory, "ega_data_rotate", 0)
    mem.ega_logical_op = getattr(src.program.memory, "ega_logical_op", 0)
    mem.ega_latches = list(getattr(src.program.memory, "ega_latches", [0, 0, 0, 0]))
    mem.ega_display_start = src.program.memory.ega_display_start

    dos = DOSMachine(src.dos.root)
    dos.stdout = list(src.dos.stdout)
    dos.files = {
        handle: FileHandle(f.path, bytearray(f.data), f.pos, f.writable)
        for handle, f in src.dos.files.items()
    }
    dos.next_handle = src.dos.next_handle
    dos.next_alloc_segment = src.dos.next_alloc_segment
    dos.allocation_limit_segment = src.dos.allocation_limit_segment
    dos.allocations = dict(src.dos.allocations)
    dos.video_mode = src.dos.video_mode
    dos.video_page = src.dos.video_page
    dos.text_mode_active = src.dos.text_mode_active
    dos.cursor_row = src.dos.cursor_row
    dos.cursor_col = src.dos.cursor_col
    dos.ticks = src.dos.ticks
    dos.vga_status_reads = src.dos.vga_status_reads
    dos.vga_palette = [tuple(rgb) for rgb in getattr(src.dos, "vga_palette", dos.vga_palette)]
    dos._dac_write_index = getattr(src.dos, "_dac_write_index", 0)
    dos._dac_read_index = getattr(src.dos, "_dac_read_index", 0)
    dos._dac_component = getattr(src.dos, "_dac_component", 0)
    dos._dac_latch = list(getattr(src.dos, "_dac_latch", []))
    dos._pit_channel2_access = getattr(src.dos, "_pit_channel2_access", 3)
    dos._pit_channel2_latch = getattr(src.dos, "_pit_channel2_latch", 0)
    dos._pit_channel2_write_low = getattr(src.dos, "_pit_channel2_write_low", True)
    dos.pit_channel2_reload = src.dos.pit_channel2_reload
    dos.speaker_control = src.dos.speaker_control
    dos.opl_selected_register = getattr(src.dos, "opl_selected_register", 0)
    dos.opl_status = getattr(src.dos, "opl_status", 0)
    dos.opl_registers = dict(getattr(src.dos, "opl_registers", {}))
    dos._seq_index = getattr(src.dos, "_seq_index", 0)
    dos._crtc_index = getattr(src.dos, "_crtc_index", 0)
    dos.current_scancode = src.dos.current_scancode
    dos.console_input_fallback = src.dos.console_input_fallback
    dos.key_queue = list(src.dos.key_queue)
    dos.port_log = list(src.dos.port_log)

    cpu = CPU8086(mem, CPUState(**src.cpu.s.__dict__))
    cpu.halted = src.cpu.halted
    cpu.trace_enabled = False
    cpu.call_depth = src.cpu.call_depth
    cpu.instruction_count = src.cpu.instruction_count
    cpu.max_rep_count = src.cpu.max_rep_count
    cpu.replacement_hooks = dict(src.cpu.replacement_hooks)
    cpu.hook_names = dict(src.cpu.hook_names)
    cpu.hook_verifier_passthrough = set(getattr(src.cpu, "hook_verifier_passthrough", set()))
    cpu.hook_verifier_live_passthrough_overrides = dict(
        getattr(src.cpu, "hook_verifier_live_passthrough_overrides", {})
    )
    cpu.hook_verifier_verify_nested_calls = getattr(src.cpu, "hook_verifier_verify_nested_calls", True)
    cpu.interrupt_handler = dos.interrupt
    cpu.port_reader = dos.port_read
    cpu.port_writer = dos.port_write

    program = copy.copy(src.program)
    program.memory = mem
    return Runtime(program, cpu, dos)


def write_runtime_repro_snapshot(
    rt: Runtime,
    *,
    root: str | Path,
    name: str,
    status: str,
    metadata: Mapping[str, Any] | None = None,
    trace_tail: Iterable[str] = (),
    timestamp: datetime | None = None,
) -> Path:
    """Write a timestamped runtime snapshot plus a small repro manifest.

    The returned directory is directly loadable with ``scripts/play.py --snapshot``.
    The additional ``repro.json`` file is intentionally best-effort metadata for
    humans/tools; the canonical VM state remains ``state.json`` + ``memory_1mb.bin``.
    """
    stamp = (timestamp or datetime.now()).strftime("%Y%m%d_%H%M%S")
    out = Path(root) / f"{safe_artifact_part(name)}_{stamp}"
    write_snapshot(rt, out, status=status, steps=rt.cpu.instruction_count, trace_tail=trace_tail)
    cs, ip = rt.cpu.addr()
    manifest = {
        "version": 1,
        "kind": "runtime_snapshot",
        "status": status,
        "snapshot": ".",
        "created_at": stamp,
        "cpu_addr": f"{cs:04X}:{ip:04X}",
        "steps": rt.cpu.instruction_count,
        "metadata": dict(metadata or {}),
    }
    (out / "repro.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return out
