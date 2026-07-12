#!/usr/bin/env python3
"""Small structural lint for the Prehistorik 2 DOS_RE fork.

Boundary rules (the BRIDGE-FREE-PRODUCT layering, task: detachable verification):

    dos_re            never imports pre2 (the framework stays game-agnostic)
    pre2.recovered    never imports pre2.bridge or dos_re (the purest layer: pure logic + pre2.views state access)
    pre2.views        never imports pre2.bridge or dos_re at TOP level (the SHIPPED state-view layer; a
                      function-local workbench import is the established fail-loud pattern)
    pre2.native       never imports pre2.bridge or dos_re at TOP level (the SHIPPED game core)
    pre2.enhanced     never imports pre2.bridge or dos_re at TOP level (SHIPPED presentation)

pre2.bridge (the VM/verification workbench: frame capture, VM fast-forwards, hook glue) is the DETACHABLE
side — it may import anything. The deployed product excludes it entirely (deploy_native.py DENY)."""
from __future__ import annotations

import ast
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
# dos_re/ is now the framework submodule's repo root; the actual package
# (the only thing this lint's game-boundary check cares about) is one level
# deeper, at dos_re/dos_re/ -- scanning the submodule root would also sweep
# in its own tests/tools/examples, which dos_re's own lint already covers.
PACKAGE_ROOTS = (ROOT / "dos_re" / "dos_re", ROOT / "pre2", ROOT / "scripts")

# SHIPPED pre2 layers -> the workbench prefixes they must not import ("top" = top-level only, lazy allowed
# under the fail-loud convention; "any" = never, even lazily).
_FORBIDDEN = ("pre2.bridge", "dos_re")
_SHIPPED_LAYERS = {
    "recovered": "any",
    "views": "top",
    "native": "top",
    "enhanced": "top",
    "codecs": "any",
}


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
        # the SHIPPED-layer boundary: product code never depends on the detachable workbench (pre2.bridge)
        # or the emulator (dos_re) — top-level always; lazily too where the layer's rule is "any".
        for layer, strictness in _SHIPPED_LAYERS.items():
            if not path.is_relative_to(ROOT / "pre2" / layer):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Import, ast.ImportFrom)):
                    continue
                if strictness == "top" and node.col_offset != 0:
                    continue                                   # lazy import: the fail-loud convention allows it
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                        ([node.module] if node.module else [])
                for name in names:
                    if any(name == f or name.startswith(f + ".") for f in _FORBIDDEN):
                        how = "" if strictness == "any" else " at top level"
                        errors.append(f"{path.relative_to(ROOT)}:{node.lineno}: shipped layer pre2.{layer} "
                                      f"must not import {name}{how} (the workbench is detachable)")
    if errors:
        print("lint failed:")
        for err in errors:
            print("  " + err)
        return 1
    print("lint passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
