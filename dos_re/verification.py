from __future__ import annotations

import copy
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from .cpu import CPU8086, CPUState
from .dos import DOSMachine, FileHandle
from .memory import (
    EGA_APERTURE,
    EGA_SHADOW_SIZE,
    Memory,
    linear,
)
from .runtime import Runtime


Addr = tuple[int, int]


def _trace_hook_target() -> Addr | None:
    """Opt-in ASM-oracle trace target from env ``OK_TRACE_HOOK="CS:IP"``.

    When set, the verifier records the ASM-oracle clone's instruction trace for
    that hook and prints it if the call diverges -- the disciplined way to see
    exactly what the original routine does that a lifted hook does not.  Returns
    None (zero overhead) when unset or malformed.
    """
    raw = os.environ.get("OK_TRACE_HOOK")
    if not raw or ":" not in raw:
        return None
    cs_text, ip_text = raw.split(":", 1)
    try:
        return int(cs_text, 16) & 0xFFFF, int(ip_text, 16) & 0xFFFF
    except ValueError:
        return None


class HookVerifyDivergence(RuntimeError):
    """Strict hook verifier mismatch with optional pre-hook repro state.

    When a lifted hook mutates the live runtime and then diverges, the caller
    must not save the already-mutated live state as the reproduction point.
    The verifier therefore attaches a clone captured immediately before the
    candidate hook was executed whenever that state is available.
    """

    def __init__(
        self,
        message: str,
        *,
        repro_runtime: Runtime | None = None,
        repro_metadata: dict[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.repro_runtime = repro_runtime
        self.repro_metadata = dict(repro_metadata or {})



class HookVerifyLimitReached(RuntimeError):
    pass


class StopRule(Protocol):
    min_steps: int

    def targets(self, cpu: CPU8086, before: CPUState) -> tuple[Addr, ...]: ...


@dataclass(frozen=True)
class GenericHookStop:
    kind: str
    ip: int | None = None
    ips: tuple[int, ...] = ()
    min_steps: int = 0

    @classmethod
    def after_step(cls, kind: str, ip: int | None = None, ips: tuple[int, ...] = ()) -> "GenericHookStop":
        return cls(kind, ip=ip, ips=ips, min_steps=1)

    def targets(self, cpu: CPU8086, before: CPUState) -> tuple[Addr, ...]:
        cs = before.cs & 0xFFFF
        if self.kind == "near_ret":
            return ((cs, cpu.mem.rw(before.ss, before.sp)),)
        if self.kind == "near_ret_or_fixed_ip":
            if self.ip is None:
                raise ValueError("near_ret_or_fixed_ip hook metadata needs ip")
            return ((cs, cpu.mem.rw(before.ss, before.sp)), (cs, self.ip & 0xFFFF))
        if self.kind == "near_ret_or_fixed_ips":
            return ((cs, cpu.mem.rw(before.ss, before.sp)),) + tuple((cs, ip & 0xFFFF) for ip in self.ips)
        if self.kind == "far_ret":
            return ((cpu.mem.rw(before.ss, (before.sp + 2) & 0xFFFF), cpu.mem.rw(before.ss, before.sp)),)
        if self.kind == "iret":
            return ((cpu.mem.rw(before.ss, (before.sp + 2) & 0xFFFF), cpu.mem.rw(before.ss, before.sp)),)
        if self.kind == "fixed_ip":
            if self.ip is None:
                raise ValueError("fixed_ip hook metadata needs ip")
            return ((cs, self.ip & 0xFFFF),)
        if self.kind == "fixed_ips":
            return tuple((cs, ip & 0xFFFF) for ip in self.ips)
        raise ValueError(f"unknown hook stop kind {self.kind!r}")


# Bytes just below SS:SP treated as dead stack scratch (popped CALL return
# words; ABI-undefined memory an interrupt may clobber).  Kept in sync with the
# unit-oracle helper ``assert_oracle_equivalent`` in game-specific hook tests.
_DEAD_STACK_BYTES = 0x40


@dataclass
class MemoryRange:
    name: str
    start: int
    size: int


@dataclass
class HookVerifierConfig:
    verify_all: bool = False
    hooks: set[Addr] = field(default_factory=set)
    max_verified: int | None = None
    stop_on_diff: bool = False
    log_diffs: bool = False
    asm_max_steps: int = 500_000
    full_memory: bool = True
    require_metadata: bool = False
    # In simple strict mode the hook side is allowed to define its own
    # continuation: run the Python replacement first, then run the original ASM
    # from the same pre-hook state until it reaches that exact CS:IP.  This is
    # deliberately dumb and slow, but removes most hand-written stop-kind
    # categorisation from small focused verification runs.
    auto_continuation: bool = False
    # Keep verification active when a lifted parent reaches/calls a child hook.
    # Disabling this restores the older faster mode where nested child hooks were
    # shared by both sides of a parent transaction and only the parent
    # continuation was diffed.
    verify_nested_hooks: bool = True
    progress_callback: Callable[[str], None] | None = None
    asm_wall_timeout_s: float | None = 20.0

    @classmethod
    def strict(
        cls,
        *,
        verify_all: bool = False,
        hooks: set[Addr] | None = None,
        max_verified: int | None = None,
        asm_max_steps: int = 1_000_000,
        progress_callback: Callable[[str], None] | None = None,
        asm_wall_timeout_s: float | None = 20.0,
    ) -> "HookVerifierConfig":
        """Create the slow, simple, fail-hard verification profile.

        Strict mode is intended for small targeted investigations, not for fast
        gameplay.  It compares the full memory image, always verifies nested
        hook boundaries, stops on the first mismatch, and uses the hook's actual
        resulting CS:IP as the ASM oracle target instead of relying on
        hand-written continuation metadata.
        """
        return cls(
            verify_all=verify_all,
            hooks=set() if hooks is None else hooks,
            max_verified=max_verified,
            stop_on_diff=True,
            log_diffs=True,
            asm_max_steps=asm_max_steps,
            full_memory=True,
            require_metadata=False,
            auto_continuation=True,
            verify_nested_hooks=True,
            progress_callback=progress_callback,
            asm_wall_timeout_s=asm_wall_timeout_s,
        )


def parse_addr(text: str) -> Addr:
    cs, ip = text.split(":", 1)
    return int(cs, 16) & 0xFFFF, int(ip, 16) & 0xFFFF


def install_hook_verifier(
    rt: Runtime,
    config: HookVerifierConfig,
    stops: dict[Addr, StopRule],
    *,
    asm_wait_handler: Callable[[CPU8086, set[Addr]], bool] | None = None,
    context_lines: Callable[[Runtime], list[str]] | None = None,
) -> "HookVerifier":
    verifier = HookVerifier(
        rt,
        config,
        stops,
        asm_wait_handler=asm_wait_handler,
        context_lines=context_lines,
    )
    rt.cpu.hook_verifier_verify_nested_calls = config.verify_nested_hooks
    rt.cpu.hook_verifier = verifier.verify
    return verifier


class HookVerifier:
    def __init__(
        self,
        rt: Runtime,
        config: HookVerifierConfig,
        stops: dict[Addr, StopRule],
        *,
        asm_wait_handler: Callable[[CPU8086, set[Addr]], bool] | None = None,
        context_lines: Callable[[Runtime], list[str]] | None = None,
    ) -> None:
        self.rt = rt
        self.config = config
        self.stops = stops
        self._asm_wait_handler = asm_wait_handler
        self._context_lines = context_lines or (lambda _rt: [])
        # Keep the hook table as it looked when verification was installed.
        # scripts/play.py installs UI pacing wrappers for a few hardware/boundary
        # hooks (50C9 retrace wait, 0679 timer wait, present blits) after creating
        # the verifier.  Those wrappers sleep, publish frames and raise control-flow
        # exceptions, which is correct for interactive execution but wrong inside a
        # differential hook transaction.  During verification, any hook listed in
        # cpu.hook_verifier_passthrough is restored from this table on the ASM
        # oracle clone.  The live hook side usually uses the same pure hook, but
        # interactive front-ends may provide live-only passthrough overrides that
        # publish frames without raising UI frame-boundary exceptions.
        self._install_time_hooks = dict(rt.cpu.replacement_hooks)
        self._install_time_names = dict(rt.cpu.hook_names)
        self.counts: dict[Addr, int] = {}
        self.total_verified = 0
        self.skipped: set[Addr] = set()

    def verify(self, cpu: CPU8086, key: Addr, handler: Callable[[CPU8086], None], name: str) -> None:
        if not self._should_verify(key):
            try:
                handler(cpu)
            finally:
                if cpu.coverage_telemetry is not None:
                    cpu.coverage_telemetry.record_hook_unverified(key, name)
            return

        call_no = self.counts.get(key, 0) + 1
        self.counts[key] = call_no
        before = CPUState(**cpu.s.__dict__)
        if self.config.progress_callback is not None:
            self.config.progress_callback(
                f"verifying {key[0]:04X}:{key[1]:04X} {name} call {call_no} "
                f"after verified={self.total_verified}"
            )

        if self.config.auto_continuation:
            self._verify_auto_continuation(cpu, key, handler, name, call_no)
            return

        stop = self.stops.get(key)
        if stop is None:
            msg = f"HOOK VERIFY MISSING METADATA {key[0]:04X}:{key[1]:04X} {name}: no continuation metadata"
            if self.config.require_metadata:
                raise HookVerifyDivergence(msg)
            if key not in self.skipped:
                print(msg.replace("MISSING METADATA", "SKIP"))
                self.skipped.add(key)
            try:
                handler(cpu)
            finally:
                if cpu.coverage_telemetry is not None:
                    cpu.coverage_telemetry.record_hook_skipped(key, name)
            return

        pre_hook_rt = self._clone_runtime()
        asm_rt = self._clone_runtime()
        asm_cpu = asm_rt.cpu
        asm_cpu.hook_verifier = None
        asm_cpu.replacement_hooks.pop(key, None)
        asm_cpu.hook_names.pop(key, None)
        targets = stop.targets(asm_cpu, before)

        self._restore_passthrough_hooks(asm_cpu)
        capture_trace = _trace_hook_target() == key
        if capture_trace:
            asm_cpu.trace_enabled = True
            asm_cpu.trace.clear()
        asm_steps = self._run_asm_to_target(
            asm_cpu,
            targets,
            min_steps=stop.min_steps,
            context=f"{key[0]:04X}:{key[1]:04X} {name} call {call_no}",
        )
        captured_trace = list(asm_cpu.trace) if capture_trace else None
        with self._live_passthrough_hooks(cpu):
            handler(cpu)
        try:
            self._finish_verified_hook(
                cpu=cpu,
                key=key,
                name=name,
                call_no=call_no,
                targets=targets,
                asm_rt=asm_rt,
                hook_rt=self.rt,
                asm_steps=asm_steps,
                pre_hook_rt=pre_hook_rt,
            )
        except HookVerifyDivergence:
            if captured_trace is not None:
                print(
                    f"=== ASM ORACLE TRACE {key[0]:04X}:{key[1]:04X} {name} "
                    f"call {call_no} ({len(captured_trace)} steps) ===",
                    flush=True,
                )
                for line in captured_trace:
                    print(line, flush=True)
                print("=== END ASM ORACLE TRACE ===", flush=True)
            raise

    def _verify_auto_continuation(
        self,
        cpu: CPU8086,
        key: Addr,
        handler: Callable[[CPU8086], None],
        name: str,
        call_no: int,
    ) -> None:
        """Strict, metadata-free verification using the hook's real CS:IP target.

        This is intentionally simple: the live hook side runs first, its final
        address becomes the only acceptable ASM-oracle continuation, and then
        the original routine is interpreted from the pre-hook clone until that
        same address is reached.  It is slow but avoids maintaining elaborate
        stop-kind metadata for focused investigations.
        """
        pre_hook_rt = self._clone_runtime()
        asm_rt = self._clone_runtime()
        asm_cpu = asm_rt.cpu
        asm_cpu.hook_verifier = None
        # Strict auto-continuation mode uses the real original ASM as the oracle,
        # not a hybrid oracle that may pass through other Python replacements.
        # The live side may still reach nested Python hooks and verify them, but
        # the reference side simply interprets the original program until it
        # reaches the hook's actual continuation address.
        asm_cpu.replacement_hooks.clear()
        asm_cpu.hook_names.clear()

        with self._live_passthrough_hooks(cpu):
            handler(cpu)

        targets = (cpu.addr(),)
        # Always execute at least one original instruction.  This prevents a
        # same-IP loop hook from being accepted against an untouched oracle just
        # because the candidate continuation equals the entry address.
        capture_trace = _trace_hook_target() == key
        if capture_trace:
            asm_cpu.trace_enabled = True
            asm_cpu.trace.clear()
        asm_steps = self._run_asm_to_target(
            asm_cpu,
            targets,
            min_steps=1,
            context=f"{key[0]:04X}:{key[1]:04X} {name} call {call_no}",
        )
        captured_trace = list(asm_cpu.trace) if capture_trace else None
        try:
            self._finish_verified_hook(
                cpu=cpu,
                key=key,
                name=name,
                call_no=call_no,
                targets=targets,
                asm_rt=asm_rt,
                hook_rt=self.rt,
                asm_steps=asm_steps,
                pre_hook_rt=pre_hook_rt,
            )
        except HookVerifyDivergence:
            if captured_trace is not None:
                print(
                    f"=== ASM ORACLE TRACE {key[0]:04X}:{key[1]:04X} {name} "
                    f"call {call_no} ({len(captured_trace)} steps) ===",
                    flush=True,
                )
                for line in captured_trace:
                    print(line, flush=True)
                print("=== END ASM ORACLE TRACE ===", flush=True)
            raise

    def _finish_verified_hook(
        self,
        *,
        cpu: CPU8086,
        key: Addr,
        name: str,
        call_no: int,
        targets: tuple[Addr, ...],
        asm_rt: Runtime,
        hook_rt: Runtime,
        asm_steps: int,
        pre_hook_rt: Runtime | None = None,
    ) -> None:
        self.total_verified += 1
        if cpu.coverage_telemetry is not None:
            cpu.coverage_telemetry.record_hook_verified(key, name, asm_steps)
        if self.config.progress_callback is not None and self.total_verified % 500 == 0:
            self.config.progress_callback(
                f"verified {self.total_verified}; last {key[0]:04X}:{key[1]:04X} {name} asm_steps={asm_steps}"
            )

        report = self._diff_report(
            key=key,
            name=name,
            call_no=call_no,
            targets=targets,
            asm_rt=asm_rt,
            hook_rt=hook_rt,
            asm_steps=asm_steps,
        )
        if report:
            if self.config.log_diffs or self.config.stop_on_diff:
                print(report)
            if self.config.stop_on_diff:
                raise HookVerifyDivergence(
                    report,
                    repro_runtime=pre_hook_rt,
                    repro_metadata={
                        "hook": f"{key[0]:04X}:{key[1]:04X}",
                        "hook_name": name,
                        "call_no": call_no,
                        "asm_steps": asm_steps,
                        "expected_continuation": self._format_targets(targets),
                    },
                )
        if self.config.max_verified is not None and self.total_verified >= self.config.max_verified:
            raise HookVerifyLimitReached(f"HOOK VERIFY LIMIT REACHED verified={self.total_verified}")
        if getattr(cpu, "hook_verifier_live_yield_requested", False):
            cpu.hook_verifier_live_yield_requested = False
            callback = getattr(cpu, "hook_verifier_live_yield_callback", None)
            if callback is not None:
                callback()

    def _should_verify(self, key: Addr) -> bool:
        if self.config.max_verified is not None and self.total_verified >= self.config.max_verified:
            return False
        return self.config.verify_all or key in self.config.hooks

    def _restore_passthrough_hooks(self, cpu: CPU8086) -> None:
        """Replace interactive passthrough wrappers with install-time base hooks.

        The pass-through set is owned by the live CPU and may be populated after
        this verifier is constructed.  That is intentional: play.py knows which
        hooks are UI pacing boundaries only after it has chosen the active video
        backend.
        """
        for key in getattr(cpu, "hook_verifier_passthrough", set()):
            if key in self._install_time_hooks:
                cpu.replacement_hooks[key] = self._install_time_hooks[key]
                if key in self._install_time_names:
                    cpu.hook_names[key] = self._install_time_names[key]
            else:
                cpu.replacement_hooks.pop(key, None)
                cpu.hook_names.pop(key, None)

    class _LivePassthroughHooks:
        def __init__(self, verifier: "HookVerifier", cpu: CPU8086) -> None:
            self.verifier = verifier
            self.cpu = cpu
            self.saved_hooks: dict[Addr, Callable[[CPU8086], None] | None] = {}
            self.saved_names: dict[Addr, str | None] = {}

        def __enter__(self) -> None:
            depth = getattr(self.cpu, "_hook_verify_live_depth", 0)
            self.cpu._hook_verify_live_depth = depth + 1
            for key in getattr(self.cpu, "hook_verifier_passthrough", set()):
                self.saved_hooks[key] = self.cpu.replacement_hooks.get(key)
                self.saved_names[key] = self.cpu.hook_names.get(key)
                live_overrides = getattr(self.cpu, "hook_verifier_live_passthrough_overrides", {})
                if key in live_overrides:
                    self.cpu.replacement_hooks[key] = live_overrides[key]
                    self.cpu.hook_names[key] = f"verify_live_passthrough_{key[0]:04X}_{key[1]:04X}"
                elif key in self.verifier._install_time_hooks:
                    self.cpu.replacement_hooks[key] = self.verifier._install_time_hooks[key]
                    if key in self.verifier._install_time_names:
                        self.cpu.hook_names[key] = self.verifier._install_time_names[key]
                else:
                    self.cpu.replacement_hooks.pop(key, None)
                    self.cpu.hook_names.pop(key, None)

        def __exit__(self, exc_type, exc, tb) -> bool:
            depth = getattr(self.cpu, "_hook_verify_live_depth", 1)
            self.cpu._hook_verify_live_depth = max(0, depth - 1)
            for key, hook in self.saved_hooks.items():
                if hook is None:
                    self.cpu.replacement_hooks.pop(key, None)
                else:
                    self.cpu.replacement_hooks[key] = hook
            for key, name in self.saved_names.items():
                if name is None:
                    self.cpu.hook_names.pop(key, None)
                else:
                    self.cpu.hook_names[key] = name
            return False

    def _live_passthrough_hooks(self, cpu: CPU8086) -> "HookVerifier._LivePassthroughHooks":
        return HookVerifier._LivePassthroughHooks(self, cpu)

    def _run_asm_to_target(
        self,
        cpu: CPU8086,
        targets: tuple[Addr, ...],
        *,
        min_steps: int = 0,
        context: str = "<unknown hook>",
    ) -> int:
        target_set = set(targets)
        min_steps = max(0, int(min_steps))
        started_at = time.monotonic()
        for steps in range(self.config.asm_max_steps + 1):
            if steps >= min_steps and cpu.addr() in target_set:
                return steps
            if self._asm_wait_handler is not None and self._asm_wait_handler(cpu, target_set):
                if steps >= min_steps and cpu.addr() in target_set:
                    return steps
                continue
            cpu.step()
            if self.config.asm_wall_timeout_s is not None:
                elapsed = time.monotonic() - started_at
                if elapsed >= self.config.asm_wall_timeout_s:
                    labels = ", ".join(f"{cs:04X}:{ip:04X}" for cs, ip in targets)
                    raise HookVerifyDivergence(
                        "HOOK VERIFY ASM WALL TIMEOUT "
                        f"hook={context} target={labels} "
                        f"after_steps={steps + 1} elapsed={elapsed:.1f}s "
                        f"at={cpu.s.cs:04X}:{cpu.s.ip:04X}"
                    )
        labels = ", ".join(f"{cs:04X}:{ip:04X}" for cs, ip in targets)
        raise HookVerifyDivergence(
            f"HOOK VERIFY ASM TIMEOUT hook={context} target={labels} "
            f"at={cpu.s.cs:04X}:{cpu.s.ip:04X}"
        )

    def _clone_runtime(self) -> Runtime:
        src = self.rt
        mem = Memory(0)
        mem.data = src.program.memory.data.copy()
        mem.size = src.program.memory.size
        mem.ega_planar = src.program.memory.ega_planar
        mem.ega_map_mask = src.program.memory.ega_map_mask
        mem.ega_read_plane = src.program.memory.ega_read_plane
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
        cpu.hook_verifier_passthrough = set(src.cpu.hook_verifier_passthrough)
        cpu.hook_verifier_live_passthrough_overrides = dict(
            getattr(src.cpu, "hook_verifier_live_passthrough_overrides", {})
        )
        cpu.hook_verifier_verify_nested_calls = getattr(
            src.cpu, "hook_verifier_verify_nested_calls", True
        )
        self._restore_passthrough_hooks(cpu)
        cpu.interrupt_handler = dos.interrupt
        cpu.port_reader = dos.port_read
        cpu.port_writer = dos.port_write

        program = copy.copy(src.program)
        program.memory = mem
        return Runtime(program, cpu, dos)

    def _memory_ranges(self, rt: Runtime) -> list[MemoryRange]:
        if self.config.full_memory:
            return [MemoryRange("full memory", 0, len(rt.program.memory.data))]

        s = rt.cpu.s
        ranges = []

        def add_range(name: str, start: int, size: int) -> None:
            start = max(0, start)
            size = max(0, size)
            if any(existing.start == start and existing.size == size for existing in ranges):
                return
            ranges.append(MemoryRange(name, start, size))

        add_range("CS:0000-FFFF", linear(s.cs, 0), 0x10000)
        add_range("DS:0000-FFFF", linear(s.ds, 0), 0x10000)
        add_range("SS:0000-FFFF", linear(s.ss, 0), 0x10000)
        add_range("CPU A000:0000-FFFF", 0xA0000, 0x10000)
        add_range("CPU B800:0000-7FFF", 0xB8000, 0x8000)
        add_range("EGA shadow planes", EGA_APERTURE, EGA_SHADOW_SIZE)
        add_range("CS:5B00-5BFF temp rows", linear(s.cs, 0x5B00), 0x0100)
        sp = s.sp & 0xFFFF
        stack_start = (sp - 0x40) & 0xFFFF
        if stack_start + 0x100 <= 0x10000:
            add_range("stack around SS:SP", linear(s.ss, stack_start), 0x100)
        return ranges

    def _diff_report(
        self,
        *,
        key: Addr,
        name: str,
        call_no: int,
        targets: tuple[Addr, ...],
        asm_rt: Runtime,
        hook_rt: Runtime,
        asm_steps: int,
    ) -> str | None:
        sections: list[str] = []
        asm_cpu = asm_rt.cpu
        hook_cpu = hook_rt.cpu

        if asm_cpu.addr() != hook_cpu.addr():
            sections.append(
                "Continuation differences:\n"
                f"  ASM:  {asm_cpu.s.cs:04X}:{asm_cpu.s.ip:04X}\n"
                f"  HOOK: {hook_cpu.s.cs:04X}:{hook_cpu.s.ip:04X}"
            )

        reg_lines = []
        for field in ("ax", "bx", "cx", "dx", "si", "di", "bp", "sp"):
            av = getattr(asm_cpu.s, field) & 0xFFFF
            hv = getattr(hook_cpu.s, field) & 0xFFFF
            if av != hv:
                reg_lines.append(f"  {field.upper()}: asm={av:04X} hook={hv:04X}")
        if reg_lines:
            sections.append("Register differences:\n" + "\n".join(reg_lines))

        seg_lines = []
        for field in ("cs", "ds", "es", "ss"):
            av = getattr(asm_cpu.s, field) & 0xFFFF
            hv = getattr(hook_cpu.s, field) & 0xFFFF
            if av != hv:
                seg_lines.append(f"  {field.upper()}: asm={av:04X} hook={hv:04X}")
        if seg_lines:
            sections.append("Segment differences:\n" + "\n".join(seg_lines))

        if (asm_cpu.s.flags & 0x0FFF) != (hook_cpu.s.flags & 0x0FFF):
            sections.append(f"Flag differences:\n  FLAGS: asm={asm_cpu.s.flags & 0x0FFF:04X} hook={hook_cpu.s.flags & 0x0FFF:04X}")

        dos_lines = self._dos_diff(asm_rt, hook_rt)
        if dos_lines:
            sections.append("DOS/state differences:\n" + "\n".join(dos_lines))

        # Ignore the dead stack scratch just below SP: a real CALL leaves its
        # popped return word there, which the calling convention defines as
        # undefined (an interrupt may overwrite it).  Lifted hooks that compose a
        # CALL/RET in Python need not reproduce that dead word, so it is not a
        # divergence.  Everything at or above SP is still compared exactly.
        ss = asm_cpu.s.ss & 0xFFFF
        sp = asm_cpu.s.sp & 0xFFFF
        dead_stack = frozenset(
            (((ss << 4) + ((sp - k) & 0xFFFF)) & 0xFFFFF)
            for k in range(1, _DEAD_STACK_BYTES + 1)
        )
        mem_sections = []
        for rng in self._memory_ranges(hook_rt):
            diff = self._range_diff(asm_rt.program.memory.data,
                                    hook_rt.program.memory.data, rng, dead_stack)
            if diff is not None:
                mem_sections.append(diff)
        if mem_sections:
            sections.append("Memory differences:\n" + "\n".join(mem_sections))

        if not sections:
            return None

        header = [
            "HOOK VERIFY DIVERGENCE",
            f"hook: {key[0]:04X}:{key[1]:04X} {name}",
            f"call: {call_no}",
            *self._context_lines(asm_rt),
            f"expected continuation: {self._format_targets(targets)}",
            f"ASM continuation: {asm_cpu.s.cs:04X}:{asm_cpu.s.ip:04X} after {asm_steps} steps",
            f"HOOK continuation: {hook_cpu.s.cs:04X}:{hook_cpu.s.ip:04X}",
        ]
        return "\n".join(header + ["", *sections])

    @staticmethod
    def _format_targets(targets: tuple[Addr, ...]) -> str:
        return ", ".join(f"{cs:04X}:{ip:04X}" for cs, ip in targets)

    def _dos_diff(self, asm_rt: Runtime, hook_rt: Runtime) -> list[str]:
        lines = []
        for field in (
            "next_handle",
            "next_alloc_segment",
            "allocation_limit_segment",
            "video_mode",
            "video_page",
            "text_mode_active",
            "cursor_row",
            "cursor_col",
            "ticks",
            "vga_status_reads",
            "_seq_index",
            "_crtc_index",
            "current_scancode",
            "console_input_fallback",
        ):
            av = getattr(asm_rt.dos, field)
            hv = getattr(hook_rt.dos, field)
            if av != hv:
                lines.append(f"  {field}: asm={av!r} hook={hv!r}")
        for field in ("allocations", "key_queue", "stdout"):
            av = getattr(asm_rt.dos, field)
            hv = getattr(hook_rt.dos, field)
            if av != hv:
                lines.append(f"  {field}: asm={av!r} hook={hv!r}")
        if asm_rt.program.memory.ega_map_mask != hook_rt.program.memory.ega_map_mask:
            lines.append(f"  ega_map_mask: asm={asm_rt.program.memory.ega_map_mask:02X} hook={hook_rt.program.memory.ega_map_mask:02X}")
        if asm_rt.program.memory.ega_read_plane != hook_rt.program.memory.ega_read_plane:
            lines.append(f"  ega_read_plane: asm={asm_rt.program.memory.ega_read_plane} hook={hook_rt.program.memory.ega_read_plane}")
        if asm_rt.program.memory.ega_display_start != hook_rt.program.memory.ega_display_start:
            lines.append(
                f"  ega_display_start: asm={asm_rt.program.memory.ega_display_start:04X} "
                f"hook={hook_rt.program.memory.ega_display_start:04X}"
            )
        for field in (
            "_pit_channel2_access",
            "_pit_channel2_latch",
            "_pit_channel2_write_low",
            "pit_channel2_reload",
            "speaker_control",
            "opl_selected_register",
            "opl_status",
            "opl_registers",
        ):
            av = getattr(asm_rt.dos, field)
            hv = getattr(hook_rt.dos, field)
            if av != hv:
                lines.append(f"  {field}: asm={av!r} hook={hv!r}")
        if asm_rt.dos.port_log != hook_rt.dos.port_log:
            lines.append(
                "  port_log_tail:\n"
                f"    asm={asm_rt.dos.port_log[-8:]}\n"
                f"    hook={hook_rt.dos.port_log[-8:]}"
            )
        lines.extend(self._file_diff(asm_rt, hook_rt))
        return lines

    @staticmethod
    def _file_diff(asm_rt: Runtime, hook_rt: Runtime) -> list[str]:
        lines = []
        asm_handles = set(asm_rt.dos.files)
        hook_handles = set(hook_rt.dos.files)
        if asm_handles != hook_handles:
            lines.append(f"  file handles: asm={sorted(asm_handles)} hook={sorted(hook_handles)}")
        for handle in sorted(asm_handles & hook_handles):
            af = asm_rt.dos.files[handle]
            hf = hook_rt.dos.files[handle]
            for field in ("path", "pos", "writable"):
                av = getattr(af, field)
                hv = getattr(hf, field)
                if av != hv:
                    lines.append(f"  file[{handle}].{field}: asm={av!r} hook={hv!r}")
            if len(af.data) != len(hf.data):
                lines.append(f"  file[{handle}].data length: asm={len(af.data)} hook={len(hf.data)}")
                continue
            if af.data != hf.data:
                first = next(i for i, (a, h) in enumerate(zip(af.data, hf.data)) if a != h)
                lines.append(
                    f"  file[{handle}].data: first diff at {first} "
                    f"asm={af.data[first]:02X} hook={hf.data[first]:02X}"
                )
        return lines

    @staticmethod
    def _range_diff(asm: bytearray, hook: bytearray, rng: MemoryRange,
                    ignore: "frozenset[int] | None" = None) -> str | None:
        start = max(0, rng.start)
        end = min(len(asm), len(hook), start + rng.size)
        asm_view = memoryview(asm)[start:end]
        hook_view = memoryview(hook)[start:end]
        if asm_view == hook_view:
            return None
        first = None
        count = 0
        for rel, (asm_byte, hook_byte) in enumerate(zip(asm_view, hook_view)):
            if asm_byte != hook_byte:
                if ignore is not None and (start + rel) in ignore:
                    continue  # dead stack scratch below SP -- ABI-undefined
                count += 1
                if first is None:
                    first = start + rel
        if first is None:
            return None
        dump_start = max(start, first - 8)
        dump_end = min(end, first + 16)
        asm_hex = " ".join(f"{b:02X}" for b in asm[dump_start:dump_end])
        hook_hex = " ".join(f"{b:02X}" for b in hook[dump_start:dump_end])
        return (
            f"  range {rng.name}:\n"
            f"    differing bytes: {count}\n"
            f"    first diff: {first:05X} asm={asm[first]:02X} hook={hook[first]:02X}\n"
            f"    asm : {asm_hex}\n"
            f"    hook: {hook_hex}"
        )

