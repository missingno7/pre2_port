"""Deploy the VM-less native game as a standalone folder (and, with --exe, a PyInstaller build).

Rips the native runtime out of the RE workbench: computes the import closure of ``scripts/play_native.py``,
refuses/skips the VM modules (the emulator must not ship), copies the closure + a launcher
into ``dist/pre2native/``, and SMOKE-TESTS the result in an isolated subprocess (sys.path = the dist folder
only) — cold-boot L1, run gameplay ticks, render a frame, and assert no VM module was imported.

The deployed folder needs only (1) Python + numpy/pygame, (2) the game data files (*.SQZ / *.TRK) from a
GOG "Prehistorik 2" install — point --game-root at it, or drop the folder contents next to the game files.

    python scripts/deploy_native.py                # build + smoke-test dist/pre2native/
    python scripts/deploy_native.py --exe          # then run PyInstaller on it (needs `pip install pyinstaller`)
    python scripts/deploy_native.py --out DIR      # custom output folder
"""
from __future__ import annotations

import argparse
import ast
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ENTRY = "scripts/play_native.py"

# The VM / RE-workbench modules that must NOT ship. The runtime path never imports them (proven by the smoke
# test); function-local imports of these in shipped files fail loud (ImportError) if a VM-needing flag is used.
DENY = (
    "pre2.runtime", "pre2.launch", "pre2.checkpoints", "pre2.probes", "pre2.replacements",
    "pre2.bootstrap_hooks", "pre2.analysis",
    "dos_re",                                       # the WHOLE emulator: native owns its VGA (pre2.native.vga)
    "play", "sdl_audio_bridge",
)

# Runtime data beyond code: NONE — the boot state is pure constants (pre2/native/boot_data.py, generated
# by pre2/probes/extract_boot_data.py). Only the user's game files (*.SQZ/*.TRK) are needed at runtime.
DATA: dict[str, str] = {}

REQUIREMENTS = "numpy\npygame\n"

README = """\
Prehistorik 2 — native port (VM-less)
=====================================

A byte-exact source reconstruction of Prehistorik 2 (GOG version), running natively in Python —
no emulator, no DOSBox. The original game data is NOT included.

Requirements
------------
* Python 3.11+ with:  pip install -r requirements.txt
* The game data from a GOG "Prehistorik 2" install (the *.SQZ / *.TRK files).

Run
---
    python pre2native.py --game-root "C:/GOG Games/Prehistorik 2"

or copy everything in this folder INTO the GOG game folder and just:

    python pre2native.py

Controls: arrows = move, Space = fire/OK, Enter/E = start, ESC = quit.
Options: --fps N (default 24), --scale N (window scale), --from-level N (skip the front-end).
"""

LAUNCHER = '''\
"""Prehistorik 2 native port — launcher. Game data (*.SQZ/*.TRK) comes from --game-root or this folder."""
import sys
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)                     # PyInstaller build
if FROZEN:
    HERE = Path(sys.executable).resolve().parent           # the exe's folder = the drop-in location
else:
    HERE = Path(__file__).resolve().parent
    sys.path.insert(0, str(HERE)); sys.path.insert(1, str(HERE / "scripts"))

if __name__ == "__main__":
    argv = sys.argv[1:]
    if not any(a.startswith("--game-root") for a in argv):
        # default: game data next to the launcher/exe (the drop-into-the-GOG-folder layout)
        root = HERE if (HERE / "SPRITES.SQZ").exists() else HERE / "assets"
        argv = ["--game-root", str(root)] + argv
    from play_native import main
    raise SystemExit(main(argv))
'''

SMOKE = '''\
"""Deployed-folder smoke test: prove the standalone runs the native game with NO VM module.
Run by deploy_native.py with cwd = the dist folder and a scrubbed PYTHONPATH, so every pre2/dos_re
import resolves INSIDE the deployed tree (the repo is not reachable)."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(1, str(HERE / "scripts"))

GAME_ROOT = sys.argv[1]
N_TICKS = int(sys.argv[2]) if len(sys.argv) > 2 else 120

from pre2.native.vga import NativeVGA                                # noqa: E402
from pre2.native.cold_boot import native_cold_boot                   # noqa: E402
from pre2.native.loop import native_gameplay_frame                   # noqa: E402
from pre2.native.render import native_render, native_sync_render_state, native_load_level_palette  # noqa: E402
import pre2.native.front_end, pre2.native.runtime, pre2.native.audio, pre2.native.game_tick_demo  # noqa: E402,F401
import play_native                                                   # noqa: E402,F401 (the runner module loads)

state = native_cold_boot(GAME_ROOT, level=0)
dos = NativeVGA()
native_load_level_palette(state, dos)
for i in range(N_TICKS):
    native_gameplay_frame(state)
native_sync_render_state(state)
DS = 0x1A0F << 4
disp = state.data[DS + 0x2DD6] | (state.data[DS + 0x2DD7] << 8)
planes, page = native_render(state, dos, disp, game_root=GAME_ROOT, force_gameplay=True)
assert len(planes) == 4 and any(any(b) for b in planes), "render produced empty planes"

DENY = {deny}
bad = sorted(m for m in sys.modules if any(m == d or m.startswith(d + ".") for d in DENY))
assert not bad, f"VM modules imported at runtime: {{bad}}"
print(f"SMOKE OK: {{N_TICKS}} ticks + render on the deployed tree, page={{page:#06x}}, VM-free")
'''


def module_to_path(mod: str) -> Path | None:
    p = ROOT / (mod.replace(".", "/") + ".py")
    if p.exists():
        return p
    p = ROOT / mod.replace(".", "/") / "__init__.py"
    if p.exists():
        return p
    p = ROOT / "scripts" / (mod + ".py")           # scripts-local bare imports (sdl_view, render_frame)
    if p.exists():
        return p
    return None


def denied(mod: str) -> bool:
    return any(mod == d or mod.startswith(d + ".") for d in DENY)


def imports_of(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    # this file's package (for resolving RELATIVE imports): pre2/native/x.py -> pre2.native
    try:
        rel = path.relative_to(ROOT).with_suffix("")
        pkg_parts = list(rel.parts[:-1]) if rel.parts[-1] != "__init__" else list(rel.parts[:-1])
    except ValueError:
        pkg_parts = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                yield a.name
        elif isinstance(n, ast.ImportFrom):
            if n.level == 0 and n.module:
                yield n.module
                for a in n.names:                                 # `from pkg import sub` — sub may be a module
                    yield f"{n.module}.{a.name}"                  # (harmless if it's a function: no file match)
            elif n.level > 0 and pkg_parts:                       # relative: from .cpu import X / from ..y import Z
                base = pkg_parts[:len(pkg_parts) - (n.level - 1)]
                if base:
                    yield ".".join(base + ([n.module] if n.module else []))
                    if not n.module:                              # from . import a, b -> the siblings themselves
                        for a in n.names:
                            yield ".".join(base + [a.name])


def compute_closure() -> list[Path]:
    todo = [ROOT / ENTRY]
    closure: set[Path] = set()
    while todo:
        p = todo.pop()
        if p in closure:
            continue
        closure.add(p)
        for m in imports_of(p):
            if denied(m):
                continue                            # VM module: not shipped; function-local uses fail loud
            mp = module_to_path(m)
            if mp is not None and mp not in closure:
                todo.append(mp)
    # package __init__.py for every shipped package dir (pre2, pre2.native, ..., dos_re)
    for p in list(closure):
        d = p.parent
        while d != ROOT and d.name:
            ini = d / "__init__.py"
            if ini.exists():
                closure.add(ini)
            d = d.parent
    return sorted(closure)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", default=str(ROOT / "dist" / "pre2native"))
    ap.add_argument("--game-root", default=str(ROOT / "assets"),
                    help="game data folder used for the smoke test (default: the repo assets/)")
    ap.add_argument("--smoke-ticks", type=int, default=120)
    ap.add_argument("--exe", action="store_true", help="also run PyInstaller (pip install pyinstaller)")
    args = ap.parse_args()
    out = Path(args.out)

    closure = compute_closure()

    def path_mod(p: Path) -> str:
        rel = p.relative_to(ROOT).with_suffix("")
        parts = list(rel.parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        return ".".join(parts)

    bad = [p for p in closure if denied(path_mod(p))]
    if bad:
        raise SystemExit(f"deny-listed files leaked into the closure: {[str(p) for p in bad]}")

    if out.exists():
        # clear CONTENTS but keep the dir: on Windows a stale handle (an old shell cwd / explorer) can pin
        # the directory inode itself while its children remain deletable
        for child in out.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    else:
        out.mkdir(parents=True)
    for p in closure:
        rel = p.relative_to(ROOT)
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, dst)
    for src, rel in DATA.items():
        sp = ROOT / src
        if not sp.exists():
            raise SystemExit(f"missing runtime data {sp} — run play_native once to build the boot image")
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sp, dst)
    (out / "pre2native.py").write_text(LAUNCHER, encoding="utf-8")
    (out / "requirements.txt").write_text(REQUIREMENTS, encoding="utf-8")
    (out / "README.txt").write_text(README, encoding="utf-8")
    (out / "_smoke.py").write_text(SMOKE.format(deny=repr(set(DENY))), encoding="utf-8")
    n_py = sum(1 for _ in out.rglob("*.py"))
    print(f"deployed {n_py} python files -> {out}")

    # ---- smoke test: run the DEPLOYED tree in a clean subprocess (imports resolve inside dist only;
    #      PYTHONPATH scrubbed so the repo tree is unreachable) ----
    print(f"smoke test: {args.smoke_ticks} native ticks + render from the deployed tree ...")
    import os
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONSTARTUP")}
    r = subprocess.run([sys.executable, str(out / "_smoke.py"), args.game_root, str(args.smoke_ticks)],
                       cwd=str(out), capture_output=True, text=True, env=env)
    sys.stdout.write(r.stdout)
    sys.stderr.write(r.stderr)
    if r.returncode != 0:
        raise SystemExit("SMOKE FAILED — the deployed tree is not standalone")
    (out / "_smoke.py").unlink()                      # scaffolding; the proof ran

    if args.exe:
        # the spec resolves its entry/data paths relative to ITSELF -> copy it beside the deployed tree
        spec = out / "pre2native.spec"
        shutil.copy2(ROOT / "scripts" / "pre2native.spec", spec)
        print("running PyInstaller ...")
        r = subprocess.run([sys.executable, "-m", "PyInstaller", "--distpath", str(out.parent / "exe"),
                            "--workpath", str(out.parent / "build"), "-y", str(spec)],
                           cwd=str(out), text=True)
        if r.returncode != 0:
            raise SystemExit("PyInstaller failed (pip install pyinstaller?)")
        print(f"exe build -> {out.parent / 'exe' / 'pre2native'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
