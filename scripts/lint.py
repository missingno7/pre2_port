#!/usr/bin/env python3
"""Small structural lint for the Prehistorik 2 DOS_RE fork."""
from __future__ import annotations

import ast
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOTS = (ROOT / "dos_re", ROOT / "pre2", ROOT / "scripts")


def iter_py_files():
    for root in PACKAGE_ROOTS:
        if not root.exists():
            continue
        for p in root.rglob("*.py"):
            if "__pycache__" not in p.parts:
                yield p


def main() -> int:
    errors: list[str] = []
    for path in iter_py_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            errors.append(f"{path.relative_to(ROOT)}: syntax error: {exc}")
            continue
        if path.is_relative_to(ROOT / "dos_re"):
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = []
                    if isinstance(node, ast.Import):
                        names = [a.name for a in node.names]
                    elif node.module:
                        names = [node.module]
                    for name in names:
                        if name == "pre2" or name.startswith("pre2."):
                            errors.append(f"{path.relative_to(ROOT)}:{node.lineno}: dos_re must not import pre2")
    if errors:
        print("lint failed:")
        for err in errors:
            print("  " + err)
        return 1
    print("lint passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
