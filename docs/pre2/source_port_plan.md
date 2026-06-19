# Prehistorik 2 source-port plan

## Non-negotiable boundary

`dos_re` is the reusable DOS machine.  It must not know Prehistorik 2 addresses, assets, command-line quirks, or source-port assumptions.

`pre2` is the game-specific layer.  It owns original PRE2 filenames, executable inventory, bootstrap helpers, future address maps, and later verified hooks.

## First milestones

1. Boot the packed `assets/pre2.exe` through the VM.
2. Treat LZEXE as bootstrap, not target game logic.
3. Collect stable snapshots after unpack/startup.
4. Identify file loads for `.sqz` and `.trk` assets.
5. Add PRE2-specific typed views only after fields are observed.
6. Replace routines only when the VM/original can verify their effects.

## Current known facts

- `pre2.exe` is an MZ executable identified by `file` as LZEXE 0.91 compressed.
- MZ entry in the packed file is `0CA6:000E`, loaded by our default PSP setup as `1CB6:000E`.
- After accelerated bootstrap, execution reaches inner code around `1996:*` / `1C34:*` in the current VM layout.
- The asset set includes dozens of `.sqz` files and `.trk` music files.

## Next technical work

- Improve AdLib/status-port timing so PRE2's sound probe/delay loops do not waste huge instruction budgets.
- Add a lightweight visual presenter boundary for early text/VGA setup.
- Trace DOS file opens/reads to map which `.sqz` files are loaded first.
- Start a `pre2/symbols.py` or JSON address ledger once stable code addresses are identified.
