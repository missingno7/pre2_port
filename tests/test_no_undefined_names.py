"""Guard against the NameError class of latent bugs: a name used in a never-exercised branch that was never
imported or defined (e.g. the food-bounce path that called ``rng_lcg`` without importing it, fixed
2026-06-28). A lightweight AST check — no external linter dependency — over the shipped recovered layers.

It over-approximates "defined" (module imports + every def/class + every assigned name anywhere + all arg
names + comprehension/except/with targets + builtins), so it only flags names defined *nowhere* in the
module — which is virtually always a real NameError waiting in an unexercised path."""
from __future__ import annotations

import ast
import builtins
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "pre2"
# the shipped recovered code (probes are throwaway scaffolding, excluded)
DIRS = ("recovered", "checkpoints", "bridge", "views", "codecs")
_BUILTINS = set(dir(builtins)) | {"__file__", "__name__", "__doc__", "__class__", "__all__", "self", "cls"}


def _modules():
    files = [ROOT / "islands.py"]
    for d in DIRS:
        files += sorted((ROOT / d).rglob("*.py"))
    return [f for f in files if f.exists() and "__pycache__" not in f.parts]


def _undefined(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"), str(path))
    defined = set(_BUILTINS)
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                defined.add((a.asname or a.name).split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            for a in n.names:
                defined.add(a.asname or a.name)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(n.name)
        elif isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            defined.add(n.id)
        elif isinstance(n, ast.arg):
            defined.add(n.arg)
        elif isinstance(n, ast.Global):
            defined.update(n.names)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            defined.add(n.name)
        elif isinstance(n, ast.withitem) and isinstance(n.optional_vars, ast.Name):
            defined.add(n.optional_vars.id)
    used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    return sorted(used - defined)


@pytest.mark.parametrize("path", _modules(), ids=lambda p: str(p.relative_to(ROOT)))
def test_no_undefined_names(path):
    undefined = _undefined(path)
    assert not undefined, f"{path.relative_to(ROOT)}: undefined name(s) {undefined} (unimported/undefined — a NameError in some branch)"
