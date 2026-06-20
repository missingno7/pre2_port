# pre2/recovered/ — clean recovered gameplay logic (the *pure* layer)

Reconstructed, **VM-independent** source-like logic for the recovered game: the
algorithms and rules the original (C-like) source had. This is where the real
source port crystallizes.

Rules:
- **No `cpu` / `mem` / `dos_re` imports.** Pure functions and dataclasses only —
  reachable and testable without the emulator.
- Functions resemble recovered original functions (e.g. `update_player`,
  `update_object`, `collision_query`, `update_frame`), operating on recovered
  dataclasses (`PlayerState`, `ObjectSlot`, `LevelState`, …) that reconstruct the
  original structs/memory layouts — not arbitrary modern abstractions.
- The VM↔dataclass translation lives in `pre2/bridge/`; the thin adapters that
  call these functions live in `pre2/replacements.py` / `pre2/checkpoints/`.

`pre2/codecs/` is the codec-specific sibling of this layer (asset decoders); it
follows the same purity rules. See `docs/pre2/recovery_architecture.md`.
