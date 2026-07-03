# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Prehistorik 2 native port — run from INSIDE the deployed folder
(scripts/deploy_native.py --exe drives this with cwd = dist/pre2native).

Produces a onedir app: dist/exe/pre2native/pre2native.exe + _internal/. The user copies the
pre2native folder's CONTENTS into their GOG "Prehistorik 2" folder (or anywhere, using
--game-root) and runs pre2native.exe. Game data is NOT bundled."""

a = Analysis(
    ["pre2native.py"],
    pathex=[".", "scripts"],                      # the deployed tree: pre2/, dos_re/, scripts/
    binaries=[],
    datas=[],                                     # nothing — the boot state is pure constants (boot_data.py)
    hiddenimports=[],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "PIL", "matplotlib"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="pre2native",
    debug=False,
    strip=False,
    upx=False,
    console=True,                                  # keep the console: fail-loud messages are the UX for now
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="pre2native",
)
