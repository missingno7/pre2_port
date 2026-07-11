"""Drift guard: scripts/overlay_menu.py is a VENDORED copy of the framework's dos_re.overlay_menu.

The overlay menu was promoted into the framework (dos_re/overlay_menu.py — the canonical, tested
implementation); pre2 keeps a byte-identical vendored copy because the APK deliberately ships no
framework code (buildozer.spec `source.exclude_dirs = ...,dos_re,...` — the native product must not
depend on the dos_re package being present), and play_native imports the menu uniformly on desktop and
device via `from overlay_menu import ...`.

One implementation, mechanically enforced: edit the menu IN dos_re (with its tests), bump the submodule,
re-copy — never edit scripts/overlay_menu.py directly. If this test fails, the two files diverged; the
fix is upstream-first, then `cp dos_re/dos_re/overlay_menu.py scripts/overlay_menu.py`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VENDORED = ROOT / "scripts" / "overlay_menu.py"
CANONICAL = ROOT / "dos_re" / "dos_re" / "overlay_menu.py"


def test_vendored_overlay_menu_matches_framework():
    if not CANONICAL.exists():
        pytest.skip("dos_re submodule not checked out (or too old to have overlay_menu)")
    assert VENDORED.read_bytes() == CANONICAL.read_bytes(), (
        "scripts/overlay_menu.py has drifted from dos_re/dos_re/overlay_menu.py — the menu is edited "
        "upstream (dos_re) and re-vendored, never locally; see this test's module docstring"
    )
