#!/usr/bin/env python3
"""Repository test runner that also works without pytest.

Use pytest for the richest local experience.  This script is the fail-safe path
for minimal automation/sandboxes: it supports tmp_path, per-test timeouts, and a
small DOS_RE smoke scope that avoids pygame, assets, and long Prehistorik 2 hook runs.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dos_re.testing import discover_tests, run_cases

SCOPES = {
    "dos-re": ["tests/test_dos_re_smoke.py"],
    "all": ["tests/test_*.py"],
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run repository tests without requiring pytest.")
    parser.add_argument(
        "patterns",
        nargs="*",
        help="optional test file globs relative to repo root; overrides --scope",
    )
    parser.add_argument(
        "--scope",
        choices=sorted(SCOPES),
        default="all",
        help="preselected test scope; 'dos-re' is quick and target-neutral",
    )
    parser.add_argument("--name", action="append", default=[], help="test function glob; may be repeated")
    parser.add_argument("--timeout", type=float, default=20.0, help="seconds per test in isolated mode")
    parser.add_argument("--in-process", action="store_true", help="faster, but disables hard per-test timeout")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--no-lint", action="store_true", help="skip scripts/lint.py before tests")
    args = parser.parse_args(argv)

    if not args.no_lint:
        lint_result = subprocess.run([sys.executable, str(ROOT / "scripts" / "lint.py")])
        if lint_result.returncode != 0:
            return lint_result.returncode

    patterns = args.patterns or SCOPES[args.scope]
    cases = discover_tests(ROOT, patterns, name_globs=tuple(args.name or ["test_*"]))
    if args.list:
        for case in cases:
            print(case.nodeid)
        print(f"{len(cases)} tests")
        return 0

    passed, failed, timed_out = run_cases(
        ROOT,
        cases,
        timeout=None if args.in_process else args.timeout,
        isolated=not args.in_process,
        fail_fast=args.fail_fast,
        verbose=args.verbose,
    )
    print(f"{passed} passed, {failed} failed, {timed_out} timed out")
    return 1 if failed or timed_out else 0


if __name__ == "__main__":
    raise SystemExit(main())
