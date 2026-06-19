"""Small dependency-free test runner used by automation and minimal sandboxes.

The project can be tested with pytest when available, but reverse-engineering
work is often done in shells that only have the Python standard library.  This
module provides the reusable runner behind ``scripts/run_tests.py``:

* direct discovery of ``test_*`` functions in ``tests/test_*.py`` files;
* a tiny ``pytest.raises`` fallback for environments without pytest;
* support for the small fixture subset used by the repository's tests
  (currently ``tmp_path``);
* optional per-test process isolation and timeout so a bad emulator loop does
  not hang the whole run.

It intentionally has no the game imports.  Game-specific scripts may choose
which test files or functions to pass in, but the runner itself is generic.
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
import argparse
import fnmatch
import importlib
import inspect
import io
import multiprocessing as mp
from pathlib import Path
import re
import sys
import tempfile
import time
import traceback
import types
from typing import Iterable, Sequence


@dataclass(frozen=True)
class TestCase:
    module: str
    function: str
    path: Path

    @property
    def nodeid(self) -> str:
        return f"{self.path.as_posix()}::{self.function}"


@dataclass
class TestResult:
    case: TestCase
    status: str
    duration: float
    output: str = ""
    error: str = ""


class _Raises:
    def __init__(self, exc_type: type[BaseException], match: str | None = None):
        self.exc_type = exc_type
        self.match = match
        self.value: BaseException | None = None

    def __enter__(self) -> "_Raises":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            raise AssertionError(f"did not raise {self.exc_type.__name__}")
        if not issubclass(exc_type, self.exc_type):
            return False
        self.value = exc
        if self.match is not None and re.search(self.match, str(exc)) is None:
            raise AssertionError(f"exception message did not match {self.match!r}: {exc}")
        return True


class _Mark:
    def __getattr__(self, name: str):
        def marker(*_args, **_kwargs):
            def decorate(obj):
                return obj
            return decorate
        return marker


class _PytestStub(types.ModuleType):
    def __init__(self) -> None:
        super().__init__("pytest")
        self.raises = _Raises
        self.mark = _Mark()

    def skip(self, reason: str = "") -> None:
        raise RuntimeError(f"pytest.skip requested in pytest-free runner: {reason}")


def ensure_pytest_stub() -> None:
    """Install a tiny pytest fallback only when real pytest is unavailable."""

    if "pytest" in sys.modules:
        return
    try:
        importlib.import_module("pytest")
    except Exception:
        sys.modules["pytest"] = _PytestStub()


def _module_name_for_path(root: Path, path: Path) -> str:
    rel = path.resolve().relative_to(root.resolve()).with_suffix("")
    return ".".join(rel.parts)


def discover_tests(
    root: Path,
    patterns: Sequence[str],
    *,
    name_globs: Sequence[str] = ("test_*",),
) -> list[TestCase]:
    cases: list[TestCase] = []
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            if not path.is_file():
                continue
            module = _module_name_for_path(root, path)
            ensure_pytest_stub()
            mod = importlib.import_module(module)
            for name in sorted(dir(mod)):
                obj = getattr(mod, name)
                if not callable(obj):
                    continue
                if not any(fnmatch.fnmatch(name, glob) for glob in name_globs):
                    continue
                cases.append(TestCase(module=module, function=name, path=path.relative_to(root)))
    return cases


def _call_test_function(root: Path, case: TestCase) -> None:
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    ensure_pytest_stub()
    mod = importlib.import_module(case.module)
    func = getattr(mod, case.function)
    sig = inspect.signature(func)
    kwargs = {}
    temp_dirs: list[tempfile.TemporaryDirectory[str]] = []
    try:
        for name in sig.parameters:
            if name == "tmp_path":
                td = tempfile.TemporaryDirectory(prefix="dos_re_test_")
                temp_dirs.append(td)
                kwargs[name] = Path(td.name)
            else:
                raise RuntimeError(
                    f"unsupported fixture/argument {name!r}; use pytest for this test "
                    "or extend dos_re.testing"
                )
        func(**kwargs)
    finally:
        for td in reversed(temp_dirs):
            td.cleanup()


def _worker(root_text: str, case: TestCase, queue: mp.Queue) -> None:
    root = Path(root_text)
    sys.path.insert(0, str(root))
    start = time.monotonic()
    out = io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(out):
            _call_test_function(root, case)
    except BaseException:
        queue.put(("FAIL", time.monotonic() - start, out.getvalue(), traceback.format_exc()))
    else:
        queue.put(("PASS", time.monotonic() - start, out.getvalue(), ""))


def run_test_case(
    root: Path,
    case: TestCase,
    *,
    timeout: float | None = 20.0,
    isolated: bool = True,
) -> TestResult:
    start = time.monotonic()
    if not isolated:
        out = io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(out):
                _call_test_function(root, case)
        except BaseException:
            return TestResult(case, "FAIL", time.monotonic() - start, out.getvalue(), traceback.format_exc())
        return TestResult(case, "PASS", time.monotonic() - start, out.getvalue(), "")

    ctx = mp.get_context("spawn" if sys.platform == "win32" else "fork")
    queue = ctx.Queue(maxsize=1)
    proc = ctx.Process(target=_worker, args=(str(root), case, queue), daemon=True)
    proc.start()
    proc.join(timeout)
    if proc.is_alive():
        proc.terminate()
        proc.join(2.0)
        if proc.is_alive() and hasattr(proc, "kill"):
            proc.kill()
            proc.join(2.0)
        return TestResult(case, "TIMEOUT", time.monotonic() - start, error=f"timed out after {timeout:.1f}s")
    if queue.empty():
        return TestResult(
            case,
            "FAIL",
            time.monotonic() - start,
            error=f"test worker exited without a result (exitcode={proc.exitcode})",
        )
    status, duration, output, error = queue.get()
    return TestResult(case, status, duration, output, error)


def run_cases(
    root: Path,
    cases: Iterable[TestCase],
    *,
    timeout: float | None = 20.0,
    isolated: bool = True,
    fail_fast: bool = False,
    verbose: bool = False,
) -> tuple[int, int, int]:
    passed = failed = timed_out = 0
    for case in cases:
        result = run_test_case(root, case, timeout=timeout, isolated=isolated)
        if result.status == "PASS":
            passed += 1
            if verbose:
                print(f"PASS {case.nodeid} ({result.duration:.2f}s)")
        else:
            if result.status == "TIMEOUT":
                timed_out += 1
            else:
                failed += 1
            print(f"{result.status} {case.nodeid} ({result.duration:.2f}s)")
            if result.output:
                print(result.output.rstrip())
            if result.error:
                print(result.error.rstrip())
            if fail_fast:
                break
    return passed, failed, timed_out


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dependency-free test runner with per-test timeout.")
    parser.add_argument(
        "patterns",
        nargs="*",
        default=["tests/test_*.py"],
        help="file globs relative to repository root; default: tests/test_*.py",
    )
    parser.add_argument(
        "--name",
        action="append",
        default=[],
        help="test function glob, e.g. 'test_dos_re_*'; may be repeated",
    )
    parser.add_argument("--timeout", type=float, default=20.0, help="seconds per test; default: 20")
    parser.add_argument("--in-process", action="store_true", help="run tests in this process; faster but no hard timeout")
    parser.add_argument("--fail-fast", action="store_true", help="stop after first failure/timeout")
    parser.add_argument("--list", action="store_true", help="list discovered tests and exit")
    parser.add_argument("--verbose", action="store_true", help="print passing tests too")
    return parser


def main(argv: Sequence[str] | None = None, *, root: Path | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    root = root or Path.cwd()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    cases = discover_tests(root, args.patterns, name_globs=tuple(args.name or ["test_*"]))
    if args.list:
        for case in cases:
            print(case.nodeid)
        print(f"{len(cases)} tests")
        return 0
    passed, failed, timed_out = run_cases(
        root,
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
