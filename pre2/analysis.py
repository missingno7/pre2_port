"""Small static helpers for discovering the Prehistorik 2 executable/assets."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from dos_re.mz import parse_mz


@dataclass(frozen=True)
class Pre2AssetInventory:
    exe: Path
    sqz_files: tuple[Path, ...]
    trk_files: tuple[Path, ...]
    docs: tuple[Path, ...]


def inventory_assets(root: str | Path) -> Pre2AssetInventory:
    root = Path(root)
    exe = root / "pre2.exe"
    return Pre2AssetInventory(
        exe=exe,
        sqz_files=tuple(sorted(root.glob("*.sqz"))),
        trk_files=tuple(sorted(root.glob("*.trk"))),
        docs=tuple(sorted(p for p in root.iterdir() if p.suffix.lower() in {".txt", ".bat", ".pif"})),
    )


def describe_exe(path: str | Path) -> dict[str, object]:
    exe = parse_mz(path)
    return {
        "path": str(path),
        "image_size": exe.header.exe_image_size,
        "load_module_size": len(exe.load_module),
        "overlay_size": len(exe.overlay),
        "relocations": len(exe.relocations),
        "entry_cs": exe.header.cs,
        "entry_ip": exe.header.ip,
        "initial_ss": exe.header.ss,
        "initial_sp": exe.header.sp,
        "min_extra_paragraphs": exe.header.min_extra_paragraphs,
        "max_extra_paragraphs": exe.header.max_extra_paragraphs,
    }
