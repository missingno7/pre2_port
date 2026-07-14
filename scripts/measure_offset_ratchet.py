"""The offset-free-release ratchet: count rb/rw/wb/ww call SITES across the shipped recovered+native closure
(docs/pre2/offset_free_release_plan.md). This is the SOURCE-level surface -- each site is one place a function
still takes/uses byte-image accessors instead of named objects/references. Distinct from
scripts/measure_name_frontier.py, which counts RUNTIME accesses (inflated by loops); this is what the plan's
"drive to 0" ratchet actually tracks.

    python scripts/measure_offset_ratchet.py
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_ACCESSOR_NAMES = {"rb", "rw", "wb", "ww"}


def count_file(path: Path) -> int:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:
        return 0
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else None
            if name in _ACCESSOR_NAMES:
                n += 1
    return n


def main() -> int:
    per_file = {}
    for base in ("pre2/recovered", "pre2/native"):
        for f in sorted((ROOT / base).glob("*.py")):
            n = count_file(f)
            if n:
                per_file[str(f.relative_to(ROOT))] = n
    total = sum(per_file.values())
    print(f"offset-ratchet (rb/rw/wb/ww call sites, pre2/recovered + pre2/native): {total}")
    for f, n in sorted(per_file.items(), key=lambda kv: -kv[1]):
        print(f"  {n:4d}  {f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
