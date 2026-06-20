"""Render DOS_RE emulator video memory to a dependency-free PNG dump.

PRE2 uses BIOS text screens, VGA DAC palettes, and a 320x200 16-colour planar
graphics path (mode 0Dh) for the game screens.  Some bring-up snapshots also
contain linear VGA mode-13h frames.  This tool reads snapshot metadata and picks
the matching decoder, while staying standard-library-only for headless evidence
inspection.

Usage:
    python scripts/render_frame.py <snapshot_dir> [--seg A000] [--out frame.png]
    python scripts/render_frame.py --steps 2000000 [--out frame.png]
"""
from __future__ import annotations

import argparse
import json
import struct
import sys
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dos_re.memory import EGA_APERTURE, EGA_PLANE_STRIDE

# The default VGA palette: the 16 standard EGA/VGA colours followed by a grey
# ramp, used only when a snapshot has no saved DAC palette.
_VGA_BASE16 = [
    (0x00, 0x00, 0x00), (0x00, 0x00, 0xAA), (0x00, 0xAA, 0x00), (0x00, 0xAA, 0xAA),
    (0xAA, 0x00, 0x00), (0xAA, 0x00, 0xAA), (0xAA, 0x55, 0x00), (0xAA, 0xAA, 0xAA),
    (0x55, 0x55, 0x55), (0x55, 0x55, 0xFF), (0x55, 0xFF, 0x55), (0x55, 0xFF, 0xFF),
    (0xFF, 0x55, 0x55), (0xFF, 0x55, 0xFF), (0xFF, 0xFF, 0x55), (0xFF, 0xFF, 0xFF),
]
DEFAULT_VGA_PALETTE = _VGA_BASE16 + [(i, i, i) for i in range(16, 256)]
WIDTH, HEIGHT = 320, 200
PLANAR_ROW_BYTES = 40  # 320 px / 8 px-per-byte, 16-colour EGA/VGA planar


def write_png(path: Path, width: int, height: int, rows: list[bytearray]) -> None:
    def chunk(typ: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + typ
            + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF)
        )

    raw = bytearray()
    for row in rows:
        raw.append(0)  # filter type 0 (none)
        raw.extend(row)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def render_vga_ppm(
    mem: bytes,
    seg: int = 0xA000,
    scale: int = 2,
    palette: list[tuple[int, int, int]] | None = None,
) -> tuple[int, int, bytes]:
    """Decode VGA mode 13h linear 320x200x8bpp memory to binary PPM (P6)."""
    base = (seg & 0xFFFF) * 16
    pal = palette if palette is not None else DEFAULT_VGA_PALETTE
    width, height = WIDTH, HEIGHT
    out = bytearray(f"P6\n{width * scale} {height * scale}\n255\n".encode("ascii"))
    for y in range(height):
        src = mem[base + y * width:base + (y + 1) * width]
        line = bytearray()
        for idx in src:
            r, g, b = pal[idx & 0xFF]
            line += bytes((r, g, b)) * scale
        out += line * scale
    return width * scale, height * scale, bytes(out)


def render_planar_ppm(
    mem: bytes,
    display_start: int = 0,
    scale: int = 2,
    palette: list[tuple[int, int, int]] | None = None,
) -> tuple[int, int, bytes]:
    """Decode 320x200 4-plane EGA/VGA memory to binary PPM (P6).

    The VM stores the four hardware planes in the shadow aperture at
    ``EGA_APERTURE``.  CRTC start address 0Ch/0Dh selects the visible byte offset
    inside those planes, so snapshots taken during PRE2 map/level scrolling must
    not be decoded from zero unconditionally.
    """
    pal = palette if palette is not None else DEFAULT_VGA_PALETTE
    start = display_start & 0xFFFF
    out = bytearray(f"P6\n{WIDTH * scale} {HEIGHT * scale}\n255\n".encode("ascii"))
    for y in range(HEIGHT):
        src_base = (start + y * PLANAR_ROW_BYTES) & 0xFFFF
        line = bytearray()
        for xbyte in range(PLANAR_ROW_BYTES):
            po = (src_base + xbyte) & 0xFFFF
            p0 = mem[EGA_APERTURE + po]
            p1 = mem[EGA_APERTURE + EGA_PLANE_STRIDE + po]
            p2 = mem[EGA_APERTURE + EGA_PLANE_STRIDE * 2 + po]
            p3 = mem[EGA_APERTURE + EGA_PLANE_STRIDE * 3 + po]
            for bit in range(7, -1, -1):
                idx = ((p0 >> bit) & 1) | (((p1 >> bit) & 1) << 1) | (((p2 >> bit) & 1) << 2) | (((p3 >> bit) & 1) << 3)
                r, g, b = pal[idx & 0x0F]
                line += bytes((r, g, b)) * scale
        out += line * scale
    return WIDTH * scale, HEIGHT * scale, bytes(out)


def load_snapshot_state(snapshot_dir: str | Path | None) -> dict:
    if snapshot_dir is None:
        return {}
    path = Path(snapshot_dir) / "state.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def state_vga_palette(state: dict) -> list[tuple[int, int, int]] | None:
    raw = state.get("dos", {}).get("vga_palette") if isinstance(state, dict) else None
    if not raw:
        return None
    return [tuple(int(c) for c in rgb) for rgb in raw]


def state_uses_planar(state: dict) -> bool:
    dos = state.get("dos", {}) if isinstance(state, dict) else {}
    return bool(dos.get("ega_planar")) or (int(dos.get("video_mode", 0)) & 0x7F) == 0x0D


def state_ega_display_start(state: dict) -> int:
    dos = state.get("dos", {}) if isinstance(state, dict) else {}
    return int(dos.get("ega_display_start", 0)) & 0xFFFF


def load_memory(args: argparse.Namespace) -> bytes:
    if args.steps is not None:
        from pre2.runtime import create_pre2_runtime

        rt = create_pre2_runtime(ROOT / "assets" / "pre2.exe", game_root=ROOT / "assets")
        rt.cpu.trace_enabled = False
        rt.cpu.run(args.steps)
        return bytes(rt.program.memory.data)
    snap = Path(args.snapshot_dir)
    return (snap / "memory_1mb.bin").read_bytes()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Render emulator VGA video memory to PNG")
    p.add_argument("snapshot_dir", nargs="?", help="snapshot directory containing memory_1mb.bin")
    p.add_argument("--steps", type=int, default=None, help="run a fresh runtime this many steps instead")
    p.add_argument("--seg", default="A000", help="VGA video segment in hex (default A000)")
    p.add_argument("--scale", type=int, default=2)
    p.add_argument("--out", default=None)
    args = p.parse_args(argv)
    if args.snapshot_dir is None and args.steps is None:
        p.error("provide a snapshot_dir or --steps")

    mem = load_memory(args)
    state = load_snapshot_state(args.snapshot_dir)
    seg = int(args.seg, 16)
    out = Path(args.out) if args.out else (
        (Path(args.snapshot_dir) / "frame.png") if args.snapshot_dir else ROOT / "frame.png"
    )
    if state_uses_planar(state):
        width, height, ppm = render_planar_ppm(
            mem,
            state_ega_display_start(state),
            args.scale,
            palette=state_vga_palette(state),
        )
        mode_label = f"EGA/VGA planar start {state_ega_display_start(state):04X}"
    else:
        width, height, ppm = render_vga_ppm(mem, seg, args.scale, palette=state_vga_palette(state))
        mode_label = f"VGA mode 13h seg {seg:04X}"
    header_end = ppm.find(b"\n255\n") + len(b"\n255\n")
    raw = ppm[header_end:]
    row_bytes = width * 3
    rows = [bytearray(raw[y * row_bytes:(y + 1) * row_bytes]) for y in range(height)]
    write_png(out, width, height, rows)
    print(f"wrote {out} ({width}x{height}, {mode_label})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
