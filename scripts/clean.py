"""Remove local generated files that should not be committed.

By default this removes Python/tool build products and local viewer dumps only.
Use ``--artifacts`` to also remove generated artifact families that are safe to
recreate and should not be kept unless promoted to ``artifacts/test_oracles`` or
``artifacts/evidence``.
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_FILE_GLOBS = (
    "*.pyc",
    "frame.png",
    "trace_start.txt",
    "checkpoint_tail.txt",
    "seg1010_*.bin",
    "tmp_*.bin",
    "tmp_*.png",
    "ega_first_present.png",
    "ega_first_present.ppm",
)

DEFAULT_DIR_GLOBS = (
    "**/__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "build",
    "dist",
    "*.egg-info",
)

ARTIFACT_DIR_GLOBS = (
    "artifacts/snapshot_play_*",
    "artifacts/play_*",
    "artifacts/tmp_*",
    "artifacts/frame_verify",
    "artifacts/verify_*",
    "artifacts/drive_menu_frames*",
    "artifacts/headless_ega_*",
    "artifacts/evidence/snapshot_play_previews",
)

ARTIFACT_FILE_GLOBS = (
    "artifacts/play_*_smoke_stdout.txt",
    "artifacts/play_*_smoke_stderr.txt",
)


def _matches(patterns: tuple[str, ...]) -> list[Path]:
    found: list[Path] = []
    for pattern in patterns:
        found.extend(ROOT.glob(pattern))
    return sorted(set(found), key=lambda p: (len(p.parts), str(p)))


def _remove_path(path: Path, *, dry_run: bool) -> None:
    rel = path.relative_to(ROOT)
    if dry_run:
        print(rel)
        return
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()
    print(rel)


def clean(*, include_artifacts: bool, dry_run: bool) -> int:
    paths = _matches(DEFAULT_FILE_GLOBS) + _matches(DEFAULT_DIR_GLOBS)
    if include_artifacts:
        paths += _matches(ARTIFACT_FILE_GLOBS) + _matches(ARTIFACT_DIR_GLOBS)

    # Remove children before parents when recursive globs return both.
    paths = sorted(set(paths), key=lambda p: len(p.parts), reverse=True)
    paths = [p for p in paths if p.exists()]
    if not paths:
        print("nothing to clean")
        return 0

    action = "would remove" if dry_run else "removed"
    print(f"{action}:")
    for path in paths:
        _remove_path(path, dry_run=dry_run)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifacts",
        action="store_true",
        help="also remove generated artifact families that are not promoted evidence/test oracles",
    )
    parser.add_argument("--dry-run", action="store_true", help="show what would be removed")
    args = parser.parse_args(argv)
    return clean(include_artifacts=args.artifacts, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
