# Package boundary

## `dos_re`

Reusable DOS reverse-engineering toolkit:

- MZ loader and PSP setup
- 8086 interpreter
- DOS/BIOS/device shims
- snapshots, input demos, hook verifier primitives
- target-neutral bootstrap accelerators such as LZEXE 0.91

`dos_re` must not import `pre2` or know Prehistorik 2 addresses/assets.

## `pre2`

Prehistorik 2-specific layer:

- original executable path and asset inventory
- launch policy
- game-specific bootstrap setup
- future PRE2 symbols, typed views, hooks, and semantic systems

## `nuked_opl3`

Vendored optional OPL backend.  It must remain independent of both `dos_re` and `pre2`.
