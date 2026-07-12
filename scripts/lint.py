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

# SHIPPED code -> the workbench prefixes it must NEVER import, not even lazily. The product's every feature
# (gameplay, tick-demo REPLAY, native snapshots, the front end) is VM-free; recording/verification live in
# the workbench, which imports the product — never the reverse.
_FORBIDDEN = ("pre2.bridge", "dos_re")
_SHIPPED_LAYERS = ("recovered", "views", "native", "enhanced", "codecs")
# the shipped entry scripts join the same boundary (scripts/ otherwise hosts the workbench CLIs)
_SHIPPED_SCRIPTS = {"play_native.py", "sdl_view.py", "render_frame.py", "overlay_menu.py",
                    "android_menu.py", "android_host.py", "main_android.py"}


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
        # the SHIPPED boundary: product code never depends on the detachable workbench (pre2.bridge) or the
        # emulator (dos_re) — not even lazily. Recording/verification import the product, never the reverse.
        shipped = any(path.is_relative_to(ROOT / "pre2" / layer) for layer in _SHIPPED_LAYERS) \
            or (path.parent == ROOT / "scripts" and path.name in _SHIPPED_SCRIPTS)
        if shipped:
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Import, ast.ImportFrom)):
                    continue
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                        ([node.module] if node.module else [])
                for name in names:
                    if any(name == f or name.startswith(f + ".") for f in _FORBIDDEN):
                        errors.append(f"{path.relative_to(ROOT)}:{node.lineno}: SHIPPED code must not import "
                                      f"{name} (the workbench is detachable; it imports the product, never "
                                      f"the reverse)")
    if errors:
        print("lint failed:")
        for err in errors:
            print("  " + err)
        return 1
    print("lint passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
