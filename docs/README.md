# Documentation

Start here:

- [`../README.md`](../README.md) — project overview; the **native VM-less game** (`play_native.py`) and how to run/ship it.
- [`../AGENTS.md`](../AGENTS.md) — agent/contributor guardrails for this repo.
- [`../ARCHITECTURE.md`](../ARCHITECTURE.md) — target layers and the VM-as-oracle direction.
- [`../dos_re/AI_PORTING_CHARTER.md`](../dos_re/AI_PORTING_CHARTER.md) — the full reusable porting method.

North-star architecture & posture:

- [`pre2/recovery_architecture.md`](pre2/recovery_architecture.md) — recovery posture, hook roles, the memory-view ↔ dataclass bridge, execution modes.
- [`pre2/recovery_lifecycle.md`](pre2/recovery_lifecycle.md) — the DOS-game recovery lifecycle and the "clean VM-less native game, VM as oracle" destination.
- [`pre2/state_view_layer.md`](pre2/state_view_layer.md) — how recovered logic reaches game state via human-named views over swappable backends (offsets out of the logic; the byte-backed adapter is a legitimate release citizen).

What is recovered (source of truth):

- [`pre2/recovered_islands.md`](pre2/recovered_islands.md) — **generated from the code** (`@oracle_link` metadata); the authoritative list of recovered islands. A test fails if it drifts.
- [`pre2/symbol_ledger.md`](pre2/symbol_ledger.md) — original-address ledger (candidate → verified).

Folders:

- `docs/dos_re/` — target-neutral VM/source-port methodology and naming discipline.
- `docs/pre2/` — Prehistorik 2-specific findings, plans, run status, and per-island/system notes. Many island
  docs (e.g. `player_fsm_island.md`, `object_system_island.md`) are **recovery-journey records** written while a
  system was being lifted; where they read as in-progress they carry a dated "historical" header and are kept for
  provenance — the *current* state of any island is [`pre2/recovered_islands.md`](pre2/recovered_islands.md) +
  [`pre2/recovery_lifecycle.md`](pre2/recovery_lifecycle.md).
- `docs/architecture/` — package-boundary and third-party notes.
