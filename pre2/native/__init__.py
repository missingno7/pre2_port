"""The VM-less faithful core — the migration destination.

`NativeGameState` (state.py) is the game's address space owned natively; `loop.py` holds the per-frame
main-loop spine and drives the recovered gameplay systems over it without a VM. This is the same recovered
code the hybrid hooks call, now invoked native-to-native: ``VM memory -> recovered fn -> VM`` becomes
``NativeGameState -> same fn -> NativeGameState``. Coverage is bounded by recovery (the spine fail-louds at the
first un-recovered routine) and grows as islands are completed. See docs/pre2/recovery_architecture.md
("Standalone (native, VM-less) — the endgame").
"""
