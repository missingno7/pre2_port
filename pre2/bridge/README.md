# pre2/bridge/ — memory views (VM memory ⇄ recovered dataclasses)

The translation layer between original VM memory and recovered structs/dataclasses
— the one place where segment:offset layout meets the recovered domain.

A memory view:
- **reads** a recovered dataclass out of live VM memory (the byte layout / field
  offsets / table bases the original game uses), and
- **writes** it back into VM memory when a recovered path is replacing the ASM.

Rules:
- Knows layout (offsets, strides, table bases) but holds **no gameplay decisions**
  — those live in `pre2/recovered/` and are called by the adapters.
- Reconstructs the original C-like structs; the dataclasses are the verification
  surface as checkpoints rise from byte/buffer diffs to semantic state contracts
  (`PlayerState`, `ObjectSlot`, `LevelState`, `CameraState`, `RendererState`,
  `GameState`).

Stood up at the first *stateful* island (sprite/tile decode → renderer). See
`docs/pre2/recovery_architecture.md` (the bidirectional bridge).
