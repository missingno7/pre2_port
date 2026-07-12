#!/usr/bin/env python3
"""The offset-ban RATCHET — measures raw memory-access "verbs" in shipped code and forbids regressions.

The object-model milestone (docs/pre2/offset_quarantine_plan.md) drives shipped gameplay code toward zero
raw DGROUP access. This lint does NOT try to decide "is this hex an offset?" (undecidable — 0xFFFF is a mask,
0x2046 a sprite id). It bans the VERBS instead: the byte-image accessors. Once the verbs are gone, stray
offset literals are inert and the remaining hex is provably arithmetic.

Counted per shipped file (recovered / views / native / enhanced / codecs + the shipped entry scripts):

    rb( rw( wb( ww(     the byte/word DGROUP accessors (call sites; ``def`` lines excluded)
    .data[              raw indexing into the 1 MB image
    DATA_SEG            the data-segment constant
    DGROUP_BASE         the linear DGROUP base

ALLOWLIST — the legitimate single homes of the machinery, which SHOULD hold the verbs:
    pre2/views/memory_adapter.py   THE byte-image adapter (readers/tile_reader/apply_ds)
    pre2/views/dgroup_view.py      THE backends + field descriptors (the view internals)

Everything else is baselined (scripts/offset_baseline.json). The lint FAILS if any file rises above its
baseline (a regression) or a new shipped file introduces verbs with no baseline. Reductions are always fine;
run ``--update`` to lower the baseline after a cleanup batch. The goal is a monotonic march to zero.

    python scripts/offset_lint.py            # check against the baseline (ratchet); prints the remaining total
    python scripts/offset_lint.py --update   # re-snapshot the baseline after a legitimate reduction
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "scripts" / "offset_baseline.json"

_SHIPPED_LAYERS = ("recovered", "views", "native", "enhanced", "codecs")
_SHIPPED_SCRIPTS = {"play_native.py", "sdl_view.py", "render_frame.py", "overlay_menu.py",
                    "android_menu.py", "android_host.py", "main_android.py"}
# the machinery's legitimate single homes — they own the verbs by design
_ALLOWLIST = {"pre2/views/memory_adapter.py", "pre2/views/dgroup_view.py"}

_VERB = re.compile(r"\b(?:rb|rw|wb|ww)\(|\.data\[|\bDATA_SEG\b|\bDGROUP_BASE\b")
_DEF = re.compile(r"\bdef\s+(?:rb|rw|wb|ww)\b")


def _shipped_files():
    files = []
    for layer in _SHIPPED_LAYERS:
        files += sorted((ROOT / "pre2" / layer).rglob("*.py"))
    for name in sorted(_SHIPPED_SCRIPTS):
        p = ROOT / "scripts" / name
        if p.exists():
            files.append(p)
    return [f for f in files if "__pycache__" not in f.parts]


def count_verbs(path: Path) -> int:
    n = 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if _DEF.search(line):
            continue                                   # a backend method DEFINITION, not an offset access
        n += len(_VERB.findall(line))
    return n


def scan() -> dict[str, int]:
    out = {}
    for f in _shipped_files():
        rel = f.relative_to(ROOT).as_posix()
        if rel in _ALLOWLIST:
            continue
        c = count_verbs(f)
        if c:
            out[rel] = c
    return out


def main() -> int:
    current = scan()
    total = sum(current.values())

    if "--update" in sys.argv[1:]:
        BASELINE.write_text(json.dumps(dict(sorted(current.items())), indent=2) + "\n", encoding="utf-8")
        print(f"offset baseline updated: {len(current)} files, {total} verbs remaining")
        return 0

    if not BASELINE.exists():
        print("no offset baseline yet — run: python scripts/offset_lint.py --update")
        return 1
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))

    regressions = []
    for rel, c in sorted(current.items()):
        base = baseline.get(rel)
        if base is None:
            regressions.append(f"  {rel}: {c} offset verbs in a NEW/clean shipped file (baseline 0)")
        elif c > base:
            regressions.append(f"  {rel}: {c} offset verbs, up from baseline {base} (+{c - base})")

    base_total = sum(baseline.values())
    if regressions:
        print("offset-lint FAILED — raw memory access regressed in shipped code:")
        print("\n".join(regressions))
        print(f"\n(baseline total {base_total}; run --update only after a genuine reduction)")
        return 1

    reduced = base_total - total
    print(f"offset-lint passed — {total} verbs across {len(current)} shipped files "
          f"(baseline {base_total}"
          + (f", down {reduced} — run --update to lock it in)" if reduced > 0 else ", no regression)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
