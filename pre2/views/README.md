# pre2/views/ — memory views (game state ⇄ recovered dataclasses)

The translation layer between the game's memory image and recovered structs/dataclasses
— the one place where segment:offset layout meets the recovered domain. SHIPPED with the
product (the VM-less runtime reads its state through these views); the endgame readability
lift pushes every remaining raw offset in gameplay code down into this layer.

A memory view:
- **reads** a recovered dataclass out of the live memory image (the byte layout / field
  offsets / table bases the original game uses), and
- **writes** it back when a recovered path is replacing the ASM.

Historical note: this layer lived at `pre2/bridge/` until the bridge-free split —
`pre2/bridge/` now holds only the DETACHABLE verification workbench (see its README).

Rules:
- Knows layout (offsets, strides, table bases) but holds **no gameplay decisions**
  — those live in `pre2/recovered/` and are called by the adapters.
- Reconstructs the original C-like structs; the dataclasses are the verification
  surface as checkpoints rise from byte/buffer diffs to semantic state contracts
  (`PlayerState`, `ObjectSlot`, `LevelState`, `CameraState`, `RendererState`,
  `GameState`).

Stood up at the first *stateful* island (sprite/tile decode → renderer). See
`docs/pre2/recovery_architecture.md` (the bidirectional bridge).
