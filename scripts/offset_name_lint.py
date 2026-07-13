#!/usr/bin/env python3
"""The offset-NAME lint — fail if any DGROUP address is a bare hex literal instead of a symbolic name.

The offset-ban ratchet (``offset_lint.py``) counts the raw-access *verbs*; this lint is the companion that
enforces the *readability* rule the naming pass established: a hex literal used as the ADDRESS argument of a
memory accessor must be a named constant (``pre2/native/dgroup_offsets.py``) or a named view — never a bare
``0x2DBC``. It uses the Python tokenizer (so comments / docstrings that mention ``[0x2d8a]`` are ignored) and
understands every accessor spelling: ``state.wb(off)``, the local helpers ``_wb(state, off)`` / ``_ww(...)``,
closures ``rw(off)``, and view-backend methods ``mem.rb(off)`` / ``ov.wb(off)``. The address argument is the
first argument, or the second when the first is a bare memory-object name (``state`` / ``mem`` / ...). The
``_cs`` accessors are code-segment, not DGROUP, so they are skipped.

    python scripts/offset_name_lint.py            # check the enforced layers (native); non-zero on any finding
    python scripts/offset_name_lint.py --survey   # also REPORT (not fail) the other layers (recovered/views)
"""
from __future__ import annotations

import io
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# every shipped layer is held to zero bare offset-hex: an accessor address must be a named constant
# (pre2/native/dgroup_offsets.py or a file-local const) or a named view — never a bare 0x2DBC.
_ENFORCED = ("native", "views", "recovered", "enhanced", "codecs")
_SURVEYED = ()   # nothing left to merely survey — the whole shipped surface is enforced

_ACCESSORS = {"rb", "rw", "wb", "ww", "rd"}
_MEM_OBJECTS = {"state", "mem", "ov", "self", "d", "view", "v"}


def _is_accessor(name: str) -> bool:
    # strip a single leading underscore (local helper), reject the _cs code-segment variants
    base = name[1:] if name.startswith("_") else name
    return base in _ACCESSORS


def offset_hits(path: Path):
    """Yield (line, hexstr) for every bare-hex ADDRESS argument of a memory accessor in ``path``."""
    src = path.read_text(encoding="utf-8", errors="replace")
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(src).readline))
    except tokenize.TokenError:
        return
    n = len(toks)
    for i, t in enumerate(toks):
        if t.type != tokenize.NAME or not _is_accessor(t.string):
            continue
        if i + 1 >= n or toks[i + 1].string != "(":
            continue
        # split the call's top-level arguments
        depth = 0
        args: list[list] = [[]]
        k = i + 1
        while k < n:
            tk = toks[k]
            if tk.string == "(":
                depth += 1
                if depth > 1:
                    args[-1].append(tk)
            elif tk.string == ")":
                depth -= 1
                if depth == 0:
                    break
                args[-1].append(tk)
            elif tk.string == "," and depth == 1:
                args.append([])
            elif depth >= 1:
                args[-1].append(tk)
            k += 1
        args = [[x for x in a if x.type not in (tokenize.NL, tokenize.NEWLINE)] for a in args]
        args = [a for a in args if a]
        if not args:
            continue
        addr_idx = 1 if (len(args[0]) == 1 and args[0][0].type == tokenize.NAME
                         and args[0][0].string in _MEM_OBJECTS) else 0
        if addr_idx >= len(args):
            continue
        first = args[addr_idx][0]
        if first.type == tokenize.NUMBER and first.string.lower().startswith("0x"):
            yield first.start[0], first.string


def _scan(layer: str):
    out = {}
    for p in sorted((ROOT / "pre2" / layer).rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        hits = list(offset_hits(p))
        if hits:
            out[p.relative_to(ROOT).as_posix()] = hits
    return out


def main() -> int:
    survey = "--survey" in sys.argv[1:]
    failed = 0
    for layer in _ENFORCED:
        found = _scan(layer)
        for rel, hits in found.items():
            for line, hx in hits:
                print(f"  {rel}:{line}: bare offset {hx} — use a dgroup_offsets symbol or a named view")
                failed += 1
    if failed:
        print(f"\noffset-name-lint FAILED — {failed} bare DGROUP offset(s) in {'/'.join(_ENFORCED)}")
        return 1
    print(f"offset-name-lint passed — no bare DGROUP offsets in {'/'.join(_ENFORCED)}")
    if survey:
        for layer in _SURVEYED:
            found = _scan(layer)
            tot = sum(len(h) for h in found.values())
            print(f"\n[survey] {layer}: {tot} bare offset-hex across {len(found)} files (not enforced)")
            for rel, hits in found.items():
                print(f"    {rel}: {len(hits)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
