"""Reusable frame-level differential verification primitives.

This module knows how to run two DOS runtimes side-by-side, stop them at
caller-provided semantic frame boundaries, compare raw/rendered samples, and
write diff artifacts.  It deliberately does not know any game-specific address,
video layout, palette, or asset path; those belong in the game adapter.
"""
from __future__ import annotations

import os
import struct
import webbrowser
import zlib
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Sequence

from .cpu import CPU8086, HaltExecution, UnsupportedInstruction
from .repro_artifacts import clone_runtime_state
from .runtime import Runtime

Addr = tuple[int, int]
FrameSource = Literal["rgb", "vram", "both"]


class FrameBoundary(Exception):
    """Internal signal raised when a semantic frame boundary is reached."""


class FrameVerifyDivergence(RuntimeError):
    """Raised when frame verification cannot continue deterministically."""


@dataclass(frozen=True)
class FrameVerifyConfig:
    """Game-independent frame verifier controls."""

    max_frames: int = 60
    frame_budget: int = 6_000_000
    source: FrameSource = "both"
    dump_dir: Path = Path("artifacts/evidence/frame_verify")
    stop_on_diff: bool = True
    preview_on_diff: bool = False
    log_every: int = 10
    trace_sample_changes: bool = False
    trace_sample_change_limit: int = 16
    trace_sample_change_start: int = 1


@dataclass
class FrameSample:
    side: str
    frame_no: int
    kind: str
    hook: Addr
    cs: int
    ip: int
    steps_since_start: int
    boundary_steps: int
    display_start: int
    raw_crc: int
    rgb_crc: int
    raw: bytes
    rgb: bytes
    recent_hooks: tuple[str, ...]
    recent_sample_changes: tuple[str, ...] = ()
    width: int = 320
    height: int = 200
    context: str = "frame"


BoundarySpec = tuple[Addr, str]
SampleBuilder = Callable[[Runtime, str, int, str, Addr, int, int, tuple[str, ...], tuple[str, ...]], FrameSample]
RuntimePairCallback = Callable[[Runtime, Runtime], None]
StopCallback = Callable[[], bool]
StatusCallback = Callable[[str], None]
PublishCallback = Callable[[Runtime, FrameSample], None]
DivergenceCallback = Callable[[Runtime, Runtime, FrameSample, FrameSample, str], None]
AfterBoundaryCallback = Callable[[Runtime, str, Addr], None]
TraceSampleCallback = Callable[[Runtime], bytes]
# Returns (kind, canonical_addr) when the CPU is parked in a boundary-less input
# wait loop, else None.  Lets the verifier treat such a loop as a frame boundary
# so demo/live input is pumped there instead of spinning until the frame budget.
InputWaitDetector = Callable[["CPU8086"], "tuple[str, Addr] | None"]


def run_frame_verifier(
    *,
    reference: Runtime,
    candidate: Runtime,
    config: FrameVerifyConfig,
    boundary_hooks: Sequence[BoundarySpec],
    sample_builder: SampleBuilder,
    reference_env_hooks: set[Addr] | frozenset[Addr] = frozenset(),
    disabled_hooks: set[Addr] | frozenset[Addr] = frozenset(),
    after_boundary: AfterBoundaryCallback | None = None,
    trace_sample: TraceSampleCallback | None = None,
    trace_sample_label: str = "sample",
    publish_candidate: PublishCallback | None = None,
    pump_inputs: RuntimePairCallback | None = None,
    on_divergence: DivergenceCallback | None = None,
    input_wait_detector: InputWaitDetector | None = None,
    stop_requested: StopCallback | None = None,
    status_callback: StatusCallback | None = None,
    label: str = "FRAME VERIFY",
) -> int:
    """Run a generic headless frame-boundary verifier.

    The caller supplies two already-initialized runtimes, the boundary addresses
    that define semantic frame/timer/retrace points, and a sample builder that
    extracts raw/rendered frame data from a runtime.
    """
    reference.cpu.trace_enabled = False
    candidate.cpu.trace_enabled = False

    _disable_hooks(reference, set(disabled_hooks))
    _disable_hooks(candidate, set(disabled_hooks))

    ref_runner = _BoundaryRunner(
        reference,
        config=config,
        side="reference",
        reference=True,
        boundary_hooks=boundary_hooks,
        reference_env_hooks=set(reference_env_hooks),
        sample_builder=sample_builder,
        after_boundary=after_boundary,
        trace_sample=None,
        trace_sample_label=trace_sample_label,
        input_wait_detector=input_wait_detector,
    )
    cand_runner = _BoundaryRunner(
        candidate,
        config=config,
        side="candidate",
        reference=False,
        boundary_hooks=boundary_hooks,
        reference_env_hooks=set(reference_env_hooks),
        sample_builder=sample_builder,
        after_boundary=after_boundary,
        trace_sample=trace_sample if config.trace_sample_changes else None,
        trace_sample_label=trace_sample_label,
        input_wait_detector=input_wait_detector,
    )

    frame_no = 1
    while config.max_frames <= 0 or frame_no <= config.max_frames:
        if stop_requested is not None and stop_requested():
            return 0
        if pump_inputs is not None:
            pump_inputs(reference, candidate)
        # Capture the pair-start state only when a caller asked for divergence
        # repros.  This lets frame verification save a snapshot before the frame
        # that first diverged instead of after the candidate has already drawn
        # the differing frame.
        pre_frame_reference = clone_runtime_state(reference) if on_divergence is not None else None
        pre_frame_candidate = clone_runtime_state(candidate) if on_divergence is not None else None
        try:
            ref_sample = ref_runner.run_to_boundary(frame_no)
            # Do not pump live input between the oracle and candidate passes.
            # Any key event collected here would reach the candidate for the
            # current frame after the reference has already advanced to its
            # boundary, producing a one-frame input skew and false visual
            # divergences while the user is actively playing.  Inputs are
            # sampled only at pair boundaries, before both runtimes advance.
            cand_sample = cand_runner.run_to_boundary(frame_no)
        except (HaltExecution, UnsupportedInstruction) as exc:
            raise FrameVerifyDivergence(
                f"{label} STOPPED before frame {frame_no}: {type(exc).__name__}: {exc}"
            ) from exc

        report = compare_samples(ref_sample, cand_sample, config, label=label)
        if publish_candidate is not None:
            publish_candidate(candidate, cand_sample)
        if report is not None:
            dump_divergence(ref_sample, cand_sample, report, config, label=label)
            print(report, flush=True)
            if status_callback is not None:
                status_callback(f"{label} divergence at frame {frame_no}")
            if on_divergence is not None and pre_frame_reference is not None and pre_frame_candidate is not None:
                on_divergence(pre_frame_reference, pre_frame_candidate, ref_sample, cand_sample, report)
            return 1 if config.stop_on_diff else 0

        if config.log_every and (frame_no == 1 or frame_no % config.log_every == 0):
            msg = (
                f"{label} ok frame={frame_no} boundary={ref_sample.kind} "
                f"raw={ref_sample.raw_crc:08X} rgb={ref_sample.rgb_crc:08X}"
            )
            print(msg, flush=True)
            if status_callback is not None:
                status_callback(msg)
        frame_no += 1

    print(f"{label} OK frames={config.max_frames}", flush=True)
    if status_callback is not None:
        status_callback(f"{label} OK frames={config.max_frames}")
    return 0


def _disable_hooks(rt: Runtime, keys: set[Addr]) -> None:
    for key in keys:
        rt.cpu.replacement_hooks.pop(key, None)
        rt.cpu.hook_names.pop(key, None)


class _BoundaryRunner:
    def __init__(
        self,
        rt: Runtime,
        *,
        config: FrameVerifyConfig,
        side: str,
        reference: bool,
        boundary_hooks: Sequence[BoundarySpec],
        reference_env_hooks: set[Addr],
        sample_builder: SampleBuilder,
        after_boundary: AfterBoundaryCallback | None,
        trace_sample: TraceSampleCallback | None,
        trace_sample_label: str,
        input_wait_detector: InputWaitDetector | None = None,
    ) -> None:
        self.rt = rt
        self.config = config
        self.input_wait_detector = input_wait_detector
        self.side = side
        self.reference = reference
        self.boundary: tuple[str, Addr, int] | None = None
        self.reference_env_hooks = reference_env_hooks
        self.sample_builder = sample_builder
        self.after_boundary = after_boundary
        self.trace_sample = trace_sample
        self.trace_sample_label = trace_sample_label
        self.recent_sample_changes: deque[str] = deque(maxlen=config.trace_sample_change_limit)
        self._base_hooks = dict(rt.cpu.replacement_hooks)
        self._base_names = dict(rt.cpu.hook_names)
        self.last_hooks: deque[str] = deque(maxlen=48)
        if reference:
            # Keep only synthetic hardware/environment hooks in the oracle.
            rt.cpu.replacement_hooks = {
                key: fn for key, fn in self._base_hooks.items() if key in reference_env_hooks
            }
            rt.cpu.hook_names = {
                key: name for key, name in self._base_names.items() if key in reference_env_hooks
            }
            self._base_hooks = dict(rt.cpu.replacement_hooks)
            self._base_names = dict(rt.cpu.hook_names)
        self._install_boundaries(boundary_hooks)
        self.rt.cpu.hook_verifier = self._trace_hook

    def _trace_hook(self, cpu: CPU8086, key: Addr, handler: Callable[[CPU8086], None], name: str) -> None:
        entry = (
            f"{cpu.instruction_count:09d} {key[0]:04X}:{key[1]:04X} {name} "
            f"enter={cpu.s.cs:04X}:{cpu.s.ip:04X}"
        )
        self.last_hooks.append(entry)
        trace_enabled = (
            self.trace_sample is not None
            and getattr(self, "frame_no", 0) >= self.config.trace_sample_change_start
        )
        before = self.trace_sample(self.rt) if trace_enabled and self.trace_sample is not None else None
        before_crc = zlib.crc32(before) & 0xFFFFFFFF if before is not None else 0
        handler(cpu)
        if before is None or self.trace_sample is None:
            return
        after = self.trace_sample(self.rt)
        if before == after:
            return
        idx = first_diff(before, after)
        changed = byte_diff_count(before, after)
        after_crc = zlib.crc32(after) & 0xFFFFFFFF
        before_byte = before[idx] if 0 <= idx < len(before) else None
        after_byte = after[idx] if 0 <= idx < len(after) else None
        byte_note = (
            f" {before_byte:02X}->{after_byte:02X}"
            if before_byte is not None and after_byte is not None else ""
        )
        self.recent_sample_changes.append(
            f"{entry} {self.trace_sample_label}_diffs={changed} "
            f"first={idx}{byte_note} crc={before_crc:08X}->{after_crc:08X}"
        )

    def _install_boundaries(self, boundary_hooks: Sequence[BoundarySpec]) -> None:
        for key, kind in boundary_hooks:
            self._install_boundary(key, kind)

    def _install_boundary(self, key: Addr, kind: str) -> None:
        base = self.rt.cpu.replacement_hooks.get(key)
        base_name = self.rt.cpu.hook_names.get(key, "replacement")

        def wrapper(
            cpu: CPU8086,
            *,
            _key: Addr = key,
            _kind: str = kind,
            _base: Callable[[CPU8086], None] | None = base,
            _base_name: str = base_name,
        ) -> None:
            start_count = cpu.instruction_count
            if self.reference and _key not in self.reference_env_hooks:
                self._run_original_near_ret(cpu, _key)
            elif _base is not None:
                _base(cpu)
            else:
                self._run_original_near_ret(cpu, _key)
            if self.after_boundary is not None:
                self.after_boundary(self.rt, _kind, _key)
            self.boundary = (_kind, _key, cpu.instruction_count - start_count)
            raise FrameBoundary()

        self.rt.cpu.replacement_hooks[key] = wrapper
        self.rt.cpu.hook_names[key] = f"frame_verify_{self.side}_{kind}"

    def _run_original_near_ret(self, cpu: CPU8086, key: Addr) -> None:
        target = (cpu.s.cs & 0xFFFF, cpu.mem.rw(cpu.s.ss, cpu.s.sp))
        saved_hook = cpu.replacement_hooks.pop(key, None)
        saved_name = cpu.hook_names.pop(key, None)
        try:
            for _ in range(self.config.frame_budget):
                if cpu.addr() == target:
                    return
                cpu.step()
        finally:
            if saved_hook is not None:
                cpu.replacement_hooks[key] = saved_hook
            if saved_name is not None:
                cpu.hook_names[key] = saved_name
        raise FrameVerifyDivergence(
            f"FRAME VERIFY ASM boundary timeout at {key[0]:04X}:{key[1]:04X}; "
            f"target={target[0]:04X}:{target[1]:04X} now={cpu.s.cs:04X}:{cpu.s.ip:04X}"
        )

    def run_to_boundary(self, frame_no: int) -> FrameSample:
        self.boundary = None
        self.frame_no = frame_no
        start = self.rt.cpu.instruction_count
        for _ in range(self.config.frame_budget):
            try:
                self.rt.cpu.step()
            except FrameBoundary:
                if self.boundary is None:
                    raise FrameVerifyDivergence("FRAME VERIFY internal error: boundary raised without metadata")
                kind, hook, boundary_steps = self.boundary
                return self.sample_builder(
                    self.rt,
                    self.side,
                    frame_no,
                    kind,
                    hook,
                    boundary_steps,
                    start,
                    tuple(self.last_hooks),
                    tuple(self.recent_sample_changes),
                )
            # A boundary-less input-wait loop (e.g. the title fire-release poll)
            # never reaches a present/timer/retrace hook, so treat it as a frame
            # boundary: return a sample here so the outer loop pumps input and
            # advances instead of spinning until the frame budget.  The detector
            # fires only at the loop's canonical head address and is checked every
            # step, so the reference and candidate both stop at the identical
            # instruction (no sub-loop desync when input is pumped here).
            if self.input_wait_detector is not None:
                detected = self.input_wait_detector(self.rt.cpu)
                if detected is not None:
                    kind, hook = detected
                    return self.sample_builder(
                        self.rt,
                        self.side,
                        frame_no,
                        kind,
                        hook,
                        self.rt.cpu.instruction_count - start,
                        start,
                        tuple(self.last_hooks),
                        tuple(self.recent_sample_changes),
                    )
        cs, ip = self.rt.cpu.addr()
        raise FrameVerifyDivergence(
            f"FRAME VERIFY TIMEOUT side={self.side} frame={frame_no} "
            f"budget={self.config.frame_budget} at={cs:04X}:{ip:04X}"
        )


def make_frame_sample(
    *,
    rt: Runtime,
    side: str,
    frame_no: int,
    kind: str,
    hook: Addr,
    boundary_steps: int,
    start_count: int,
    recent_hooks: tuple[str, ...],
    recent_sample_changes: tuple[str, ...] = (),
    raw: bytes = b"",
    rgb: bytes = b"",
    display_start: int = 0,
    width: int = 320,
    height: int = 200,
    context: str = "frame",
) -> FrameSample:
    return FrameSample(
        side=side,
        frame_no=frame_no,
        kind=kind,
        hook=hook,
        cs=rt.cpu.s.cs & 0xFFFF,
        ip=rt.cpu.s.ip & 0xFFFF,
        steps_since_start=rt.cpu.instruction_count - start_count,
        boundary_steps=boundary_steps,
        display_start=display_start,
        raw_crc=zlib.crc32(raw) & 0xFFFFFFFF,
        rgb_crc=zlib.crc32(rgb) & 0xFFFFFFFF,
        raw=raw,
        rgb=rgb,
        recent_hooks=recent_hooks,
        recent_sample_changes=recent_sample_changes,
        width=width,
        height=height,
        context=context,
    )


def compare_samples(
    ref: FrameSample,
    cand: FrameSample,
    config: FrameVerifyConfig,
    *,
    label: str = "FRAME VERIFY",
) -> str | None:
    sections: list[str] = []
    if ref.width != cand.width or ref.height != cand.height:
        sections.append(
            "Frame geometry differences:\n"
            f"  REF:  {ref.width}x{ref.height}\n"
            f"  HOOK: {cand.width}x{cand.height}"
        )
    if ref.kind != cand.kind or ref.hook != cand.hook:
        sections.append(
            "Boundary differences:\n"
            f"  REF:  {ref.kind} {ref.hook[0]:04X}:{ref.hook[1]:04X}\n"
            f"  HOOK: {cand.kind} {cand.hook[0]:04X}:{cand.hook[1]:04X}"
        )
    if ref.display_start != cand.display_start:
        sections.append(f"Display start differences:\n  REF: {ref.display_start:04X}\n  HOOK: {cand.display_start:04X}")
    if config.source in ("vram", "both") and ref.raw != cand.raw:
        idx = first_diff(ref.raw, cand.raw)
        sections.append(
            "Raw video differences:\n"
            f"  REF crc:  {ref.raw_crc:08X}\n"
            f"  HOOK crc: {cand.raw_crc:08X}\n"
            f"  first differing byte: {idx}"
        )
    if config.source in ("rgb", "both") and ref.rgb != cand.rgb:
        idx = first_diff(ref.rgb, cand.rgb)
        pixel = idx // 3 if idx >= 0 else -1
        y, x = divmod(pixel, ref.width) if pixel >= 0 and ref.width else (-1, -1)
        sections.append(
            "Rendered RGB differences:\n"
            f"  REF crc:  {ref.rgb_crc:08X}\n"
            f"  HOOK crc: {cand.rgb_crc:08X}\n"
            f"  first differing pixel: x={x} y={y} channel={idx % 3 if idx >= 0 else -1}"
        )
    if not sections:
        return None
    hook_tail = "\n".join(f"  {line}" for line in cand.recent_hooks[-16:]) or "  <none>"
    change_tail = "\n".join(f"  {line}" for line in cand.recent_sample_changes[-16:]) or "  <not enabled>"
    ref_tail = "\n".join(f"  {line}" for line in ref.recent_hooks[-8:]) or "  <none>"
    return (
        f"{label} DIVERGENCE\n"
        f"frame: {ref.frame_no}\n"
        f"context: {ref.context}\n"
        f"source: {config.source}\n"
        f"REF continuation:  {ref.cs:04X}:{ref.ip:04X} steps={ref.steps_since_start}\n"
        f"HOOK continuation: {cand.cs:04X}:{cand.ip:04X} steps={cand.steps_since_start}\n"
        + "\n\n".join(sections)
        + "\n\nRecent candidate hooks before divergence:\n" + hook_tail
        + "\n\nRecent candidate sample-changing hooks before divergence:\n" + change_tail
        + "\n\nRecent reference hooks before divergence:\n" + ref_tail
    )


def first_diff(a: bytes, b: bytes) -> int:
    n = min(len(a), len(b))
    for i in range(n):
        if a[i] != b[i]:
            return i
    if len(a) != len(b):
        return n
    return -1


def byte_diff_count(a: bytes, b: bytes) -> int:
    n = min(len(a), len(b))
    count = sum(1 for i in range(n) if a[i] != b[i])
    return count + abs(len(a) - len(b))


def dump_divergence(
    ref: FrameSample,
    cand: FrameSample,
    report: str,
    config: FrameVerifyConfig,
    *,
    label: str = "FRAME VERIFY",
) -> None:
    out = config.dump_dir
    out.mkdir(parents=True, exist_ok=True)
    stem = f"frame_{ref.frame_no:05d}_{ref.context}"
    (out / f"{stem}_report.txt").write_text(report + "\n", encoding="utf-8")
    meta = {
        "frame": ref.frame_no,
        "context": ref.context,
        "source": config.source,
        "reference": sample_meta(ref),
        "candidate": sample_meta(cand),
    }
    import json

    (out / f"{stem}_report.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    (out / f"{stem}_ref_vram.bin").write_bytes(ref.raw)
    (out / f"{stem}_hook_vram.bin").write_bytes(cand.raw)
    write_rgb_png(out / f"{stem}_ref.png", ref.rgb, width=ref.width, height=ref.height)
    write_rgb_png(out / f"{stem}_hook.png", cand.rgb, width=cand.width, height=cand.height)
    diff_rgb = diff_rgb_frame(ref.rgb, cand.rgb)
    write_rgb_png(out / f"{stem}_diff.png", diff_rgb, width=ref.width, height=ref.height)
    compare_rgb = compose_compare_rgb(ref.rgb, cand.rgb, diff_rgb, width=ref.width, height=ref.height)
    compare_width = ref.width * 3 + 8
    write_rgb_png(out / f"{stem}_compare.png", compare_rgb, width=compare_width, height=ref.height)
    compare_path = out / f"{stem}_compare.png"
    print(f"{label} artifacts written to {out / stem}_*", flush=True)
    print(f"{label} compare image: {compare_path}", flush=True)
    if config.preview_on_diff:
        open_image(compare_path)


def sample_meta(sample: FrameSample) -> dict[str, object]:
    return {
        "side": sample.side,
        "frame_no": sample.frame_no,
        "kind": sample.kind,
        "hook": f"{sample.hook[0]:04X}:{sample.hook[1]:04X}",
        "continuation": f"{sample.cs:04X}:{sample.ip:04X}",
        "steps_since_start": sample.steps_since_start,
        "boundary_steps": sample.boundary_steps,
        "display_start": f"{sample.display_start:04X}",
        "raw_crc": f"{sample.raw_crc:08X}",
        "rgb_crc": f"{sample.rgb_crc:08X}",
        "width": sample.width,
        "height": sample.height,
        "context": sample.context,
        "recent_hooks": list(sample.recent_hooks),
        "recent_sample_changes": list(sample.recent_sample_changes),
    }


def diff_rgb_frame(a: bytes, b: bytes) -> bytes:
    out = bytearray(len(a))
    npx = min(len(a), len(b)) // 3
    for p in range(npx):
        i = p * 3
        changed = a[i:i + 3] != b[i:i + 3]
        if changed:
            out[i:i + 3] = b"\xff\xff\xff"
        else:
            out[i:i + 3] = b"\x00\x00\x00"
    if len(a) != len(b):
        for i in range(npx * 3, len(out)):
            out[i] = 0xFF
    return bytes(out)


def compose_compare_rgb(ref_rgb: bytes, cand_rgb: bytes, diff_rgb: bytes, *, width: int = 320, height: int = 200) -> bytes:
    """Pack reference, candidate, and diff frames into one side-by-side image."""
    if not (len(ref_rgb) == len(cand_rgb) == len(diff_rgb)):
        raise ValueError("compare RGB buffers must be the same length")
    row_bytes = width * 3
    if len(ref_rgb) != row_bytes * height:
        raise ValueError(f"expected {row_bytes * height} RGB bytes per frame, got {len(ref_rgb)}")

    separator = b"\x20\x20\x20" * 4
    out = bytearray()
    for y in range(height):
        off = y * row_bytes
        out.extend(ref_rgb[off:off + row_bytes])
        out.extend(separator)
        out.extend(cand_rgb[off:off + row_bytes])
        out.extend(separator)
        out.extend(diff_rgb[off:off + row_bytes])
    return bytes(out)


def open_image(path: Path) -> None:
    """Best-effort open of a rendered comparison artifact."""
    try:
        if hasattr(os, "startfile"):
            os.startfile(str(path))  # type: ignore[attr-defined]
            return
        webbrowser.open(path.as_uri())
    except Exception as exc:  # pragma: no cover - best-effort convenience only
        print(f"FRAME VERIFY preview failed for {path}: {type(exc).__name__}: {exc}", flush=True)


def write_rgb_png(path: Path, rgb: bytes, *, width: int = 320, height: int = 200) -> None:
    expected = width * height * 3
    if len(rgb) != expected:
        raise ValueError(f"expected {expected} RGB bytes, got {len(rgb)}")

    def chunk(typ: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + typ
            + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
        )

    row_bytes = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0 (none)
        raw.extend(rgb[y * row_bytes:(y + 1) * row_bytes])

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)
