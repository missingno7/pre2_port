"""Reusable deterministic input-demo recording and playback for DOS runtimes.

The recorder stores a start snapshot plus VM-visible input events keyed by an
emulated boundary counter.  It intentionally does not know about the game, SDL,
video modes, or a particular game.  Front-ends provide a demo name, metadata,
and the boundary at which host input is delivered.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .interrupts import deliver_scancode
from .runtime import Runtime
from .snapshot import write_snapshot

DEMO_VERSION = 1


@dataclass(frozen=True)
class InputDemoEvent:
    boundary: int
    seq: int
    kind: str
    value: int | None = None
    scancode: int | None = None
    text: str = ""

    @classmethod
    def from_json(cls, raw: dict) -> "InputDemoEvent":
        return cls(
            boundary=max(0, int(raw.get("boundary", 0))),
            seq=max(0, int(raw.get("seq", 0))),
            kind=str(raw.get("kind", "")),
            value=None if raw.get("value") is None else int(raw["value"]) & 0xFFFF,
            scancode=None if raw.get("scancode") is None else int(raw["scancode"]) & 0xFF,
            text=str(raw.get("text", "")),
        )

    def to_json(self) -> dict:
        out: dict[str, int | str] = {"boundary": self.boundary, "seq": self.seq, "kind": self.kind}
        if self.value is not None:
            out["value"] = self.value & 0xFFFF
        if self.scancode is not None:
            out["scancode"] = self.scancode & 0xFF
        if self.text:
            out["text"] = self.text
        return out


class InputDemoRecorder:
    """Record a start snapshot plus VM-visible keyboard events.

    ``name`` is only used for the output directory prefix.  ``metadata`` is
    copied verbatim into the manifest so a game front-end can record things
    like video mode, sound mode, command tail, or executable identity without
    making the demo format game-specific.
    """

    def __init__(
        self,
        *,
        root: Path,
        name: str,
        metadata: dict[str, object] | None = None,
        snapshot_name: str = "snapshot",
    ) -> None:
        self.root = Path(root)
        self.name = _safe_demo_name(name)
        self.metadata = dict(metadata or {})
        self.snapshot_name = snapshot_name
        self.demo_dir: Path | None = None
        self.snapshot_dir: Path | None = None
        self.start_boundary = 0
        self._seq = 0
        self._events: list[InputDemoEvent] = []
        self._started_at = ""
        self._stopped_at = ""

    @property
    def active(self) -> bool:
        return self.demo_dir is not None

    @property
    def event_count(self) -> int:
        return len(self._events)

    def start(self, rt: Runtime, *, boundary: int) -> Path:
        if self.active:
            raise RuntimeError("input demo recording is already active")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.demo_dir = self.root / f"demo_{self.name}_{stamp}"
        self.snapshot_dir = self.demo_dir / self.snapshot_name
        self.demo_dir.mkdir(parents=True, exist_ok=True)
        self.start_boundary = max(0, int(boundary))
        self._seq = 0
        self._events.clear()
        self._started_at = datetime.now().isoformat(timespec="seconds")
        self._stopped_at = ""
        write_snapshot(rt, self.snapshot_dir, status="input demo start snapshot", steps=rt.cpu.instruction_count, trace_tail=())
        self._write_manifest(final=False)
        return self.demo_dir

    def record_scan(self, *, boundary: int, scancode: int) -> None:
        if not self.active:
            return
        self._append(InputDemoEvent(boundary=self._relative_boundary(boundary), seq=self._seq, kind="scan", value=scancode & 0xFF))

    def record_dos_key(self, *, boundary: int, scancode: int, text: str, value: int) -> None:
        if not self.active:
            return
        self._append(InputDemoEvent(
            boundary=self._relative_boundary(boundary),
            seq=self._seq,
            kind="dos_key",
            value=value & 0xFFFF,
            scancode=scancode & 0xFF,
            text=text[:1],
        ))

    def stop(self, *, boundary: int) -> Path:
        if not self.active or self.demo_dir is None:
            raise RuntimeError("input demo recording is not active")
        self._stopped_at = datetime.now().isoformat(timespec="seconds")
        self._write_manifest(final=True, end_boundary=self._relative_boundary(boundary))
        out = self.demo_dir
        self.demo_dir = None
        self.snapshot_dir = None
        return out

    def _relative_boundary(self, boundary: int) -> int:
        return max(0, int(boundary) - self.start_boundary)

    def _append(self, event: InputDemoEvent) -> None:
        self._events.append(event)
        self._seq += 1
        self._write_manifest(final=False)

    def _write_manifest(self, *, final: bool, end_boundary: int | None = None) -> None:
        if self.demo_dir is None:
            return
        manifest = {
            "version": DEMO_VERSION,
            "status": "complete" if final else "recording",
            "created_at": self._started_at,
            "stopped_at": self._stopped_at,
            "snapshot": self.snapshot_name,
            "metadata": self.metadata,
            "start_boundary": 0,
            "end_boundary": end_boundary,
            "event_count": len(self._events),
            "events": [event.to_json() for event in self._events],
        }
        (self.demo_dir / "input_demo.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")


class InputDemoPlayback:
    """Replay a recorded input demo into one or more runtimes."""

    def __init__(self, *, demo_dir: Path, manifest: dict) -> None:
        self.demo_dir = demo_dir
        self.manifest = manifest
        self.events = sorted((InputDemoEvent.from_json(raw) for raw in manifest.get("events", [])), key=lambda e: (e.boundary, e.seq))
        self._index = 0

    @classmethod
    def load(cls, path: str | Path) -> "InputDemoPlayback":
        p = Path(path)
        if p.is_dir():
            manifest_path = p / "input_demo.json"
            demo_dir = p
        else:
            manifest_path = p
            demo_dir = p.parent
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if int(manifest.get("version", 0)) != DEMO_VERSION:
            raise ValueError(f"unsupported input demo version: {manifest.get('version')!r}")
        return cls(demo_dir=demo_dir, manifest=manifest)

    def snapshot_path(self) -> Path:
        path = Path(str(self.manifest.get("snapshot", "snapshot")))
        if not path.is_absolute():
            path = self.demo_dir / path
        return path

    @property
    def next_event_index(self) -> int:
        """Index of the first recorded event that has not yet been replayed."""
        return self._index

    def remaining_events_from_cursor(self, *, boundary: int) -> list[InputDemoEvent]:
        """Return unapplied events re-keyed to a new snapshot at ``boundary``.

        Use the playback cursor rather than filtering by ``event.boundary``.
        ``apply_to_runtime`` consumes all events whose boundary is ``<=`` the
        current boundary, so a same-boundary release/key event may already be
        applied even though its original boundary equals the suffix start.
        """
        base = max(0, int(boundary))
        out: list[InputDemoEvent] = []
        for seq, event in enumerate(self.events[self._index:]):
            out.append(InputDemoEvent(
                boundary=max(0, event.boundary - base),
                seq=seq,
                kind=event.kind,
                value=event.value,
                scancode=event.scancode,
                text=event.text,
            ))
        return out

    def write_suffix(
        self,
        rt: Runtime,
        *,
        root: Path,
        name: str,
        boundary: int,
        status: str,
        metadata: dict[str, object] | None = None,
        snapshot_name: str = "snapshot",
        trace_tail: Iterable[str] = (),
    ) -> Path:
        """Write a new demo that starts from ``rt`` and replays remaining input.

        This is the reproducibility helper for long demos: save a snapshot at
        the current VM point and copy only the not-yet-applied events from the
        original demo, rebased to the new snapshot's boundary zero.
        """
        root = Path(root)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = root / f"demo_{_safe_demo_name(name)}_{stamp}"
        snapshot_dir = out / snapshot_name
        out.mkdir(parents=True, exist_ok=True)
        write_snapshot(rt, snapshot_dir, status=status, steps=rt.cpu.instruction_count, trace_tail=trace_tail)

        base_boundary = max(0, int(boundary))
        end = self.end_boundary
        suffix_end = None if end is None else max(0, int(end) - base_boundary)
        source_metadata = dict(self.manifest.get("metadata", {}))
        suffix_metadata = {
            **source_metadata,
            **dict(metadata or {}),
            "source_demo": str(self.demo_dir),
            "source_boundary": base_boundary,
            "source_next_event_index": self._index,
            "source_event_count": len(self.events),
            "suffix_kind": "remaining_input_from_cursor",
        }
        events = self.remaining_events_from_cursor(boundary=base_boundary)
        manifest = {
            "version": DEMO_VERSION,
            "status": "complete",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "stopped_at": datetime.now().isoformat(timespec="seconds"),
            "snapshot": snapshot_name,
            "metadata": suffix_metadata,
            "start_boundary": 0,
            "end_boundary": suffix_end,
            "event_count": len(events),
            "events": [event.to_json() for event in events],
        }
        (out / "input_demo.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        return out

    def reset(self) -> None:
        self._index = 0

    @property
    def exhausted(self) -> bool:
        return self._index >= len(self.events)

    @property
    def end_boundary(self) -> int | None:
        """Boundary at which recording stopped, if the manifest recorded one.

        Older demos predate the field; callers fall back to :attr:`exhausted`.
        """
        raw = self.manifest.get("end_boundary")
        return None if raw is None else max(0, int(raw))

    def finished(self, boundary: int) -> bool:
        """Whether replay has reached the end of the recorded demo.

        Prefer the recorded ``end_boundary`` so trailing idle frames (recorded
        after the last key event) still play back before stopping; fall back to
        "all events applied" for demos that have no end boundary.
        """
        end = self.end_boundary
        if end is not None:
            return boundary >= end
        return self.exhausted

    def apply_to_runtime(self, boundary: int, rt: Runtime, *, deliver: Callable[[Runtime, int], None] = deliver_scancode) -> int:
        return self.apply_to_runtimes(boundary, (rt,), deliver=deliver)

    def apply_to_runtimes(self, boundary: int, runtimes: Sequence[Runtime], *, deliver: Callable[[Runtime, int], None] = deliver_scancode) -> int:
        boundary = max(0, int(boundary))
        applied = 0
        while self._index < len(self.events) and self.events[self._index].boundary <= boundary:
            event = self.events[self._index]
            for rt in runtimes:
                self._apply_event(rt, event, deliver=deliver)
            self._index += 1
            applied += 1
        return applied

    @staticmethod
    def _apply_event(rt: Runtime, event: InputDemoEvent, *, deliver: Callable[[Runtime, int], None]) -> None:
        if event.kind == "scan":
            if event.value is None:
                raise ValueError("scan demo event missing value")
            deliver(rt, event.value & 0xFF)
        elif event.kind == "dos_key":
            if event.value is None:
                raise ValueError("dos_key demo event missing value")
            rt.dos.key_queue.append(event.value & 0xFFFF)
        else:
            raise ValueError(f"unknown input demo event kind: {event.kind!r}")


def bios_key_value_from_scancode(scancode: int, text: str) -> int | None:
    if not text:
        text = {
            0x02: "1", 0x03: "2", 0x04: "3", 0x05: "4", 0x06: "5", 0x07: "6", 0x08: "7", 0x09: "8", 0x0A: "9", 0x0B: "0",
            0x0C: "-", 0x0D: "=", 0x0E: "\b", 0x0F: "\t",
            0x10: "q", 0x11: "w", 0x12: "e", 0x13: "r", 0x14: "t", 0x15: "y", 0x16: "u", 0x17: "i", 0x18: "o", 0x19: "p",
            0x1A: "[", 0x1B: "]", 0x1C: "\r",
            0x1E: "a", 0x1F: "s", 0x20: "d", 0x21: "f", 0x22: "g", 0x23: "h", 0x24: "j", 0x25: "k", 0x26: "l", 0x27: ";",
            0x28: "'", 0x29: "`", 0x2B: "\\",
            0x2C: "z", 0x2D: "x", 0x2E: "c", 0x2F: "v", 0x30: "b", 0x31: "n", 0x32: "m", 0x33: ",", 0x34: ".", 0x35: "/",
            0x39: " ", 0x01: "\x1b",
        }.get(scancode & 0xFF, "")
    if not text:
        return None
    ch = ord(text[0])
    if ch < 0x20 and ch not in (0x08, 0x09, 0x0D, 0x1B):
        return None
    return (((scancode & 0xFF) << 8) | (ch & 0xFF)) & 0xFFFF


# Backwards-compatible alias used by existing front-ends/tests.
dos_key_value = bios_key_value_from_scancode


def _safe_demo_name(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in str(name).strip())
    return cleaned or "input"
