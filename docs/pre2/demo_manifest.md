# PRE2 demo manifest — what each recorded demo is + which enemy types it witnesses

Demos live in `artifacts/demo_pre2_<timestamp>/` (referenced by timestamp). The "handler types" column is the
object-AI handler indices (`idx0..idx23`, the `CS:0x6AA9` dispatch table) that fire when the demo replays —
use it to pick a demo that witnesses a given enemy type for shadow-verification. Regenerate with
`python pre2/probes/demo_census.py`. `*` = not yet recovered.

| demo (timestamp) | content | handler types witnessed |
|---|---|---|
| 20260625_141459 | (early) | idx9 |
| 20260626_001406 | (early) | idx1, idx3 |
| 20260626_001416 | (early) | idx1, idx4 |
| 20260626_001424 | L1 (early) | idx1, idx4 |
| 20260626_001513 | L1 (early) | idx1, idx2, idx10 |
| 20260626_102854 | **L1** | idx1, idx2, idx10 |
| 20260626_105203 | **gorilla boss** | idx9 |
| 20260626_105310 | **earthquake / rising tiles** | idx6*, idx9, idx12* |
| 20260626_105529 | (gameplay) | idx10 |
| 20260626_105730 | **tiger enemy** | idx8 |
| 20260626_111734 | **L5 expert** | idx0, idx1, idx2, idx4, **idx7**, idx9 |
| 20260626_112134 | **L4 expert** | idx0, idx8 |
| 20260626_112253 | **L6 expert** | idx1, idx2, idx3, idx4, idx8, idx9 |
| 20260626_112428 | **L7 expert (penguins)** | idx1, idx3, idx4, idx9, idx12* |
| 20260626_115215 | (squirrel area) | idx11 |
| 20260626_115310 | **flying squirrel** | idx11 |
| 20260626_115441 | (gameplay) | idx10 |
| 20260626_115452 | (gameplay) | idx9, idx10 |
| 20260626_123017 | **tree boss defeat** | idx10 |

## Coverage summary
- **Witnessed** (in at least one demo): idx 0,1,2,3,4,6,7,8,9,10,11,12.
- **Recovered + shadow-verified**: idx 0,1,2,3,4,6,7,8,9,10,11,12 — i.e. ALL witnessed types
  (`pre2/recovered/object_update.py`). idx6 (earthquake/screen-shake) also drives the global shake state
  `[0xA30E/0xA310/0x6BC0/0x6BC1]` and the two PRNGs (`pre2/recovered/prng.py`); its full contract is shadowed
  in `pre2/probes/probe_handler_shadow.py`.
- **NEVER witnessed in any demo** (need their level, or recover from disasm without a shadow): idx5 (`7A60`),
  idx13–23.
- Bosses observed so far use ordinary recovered handlers (gorilla=idx9 patrol, tree boss=idx10 charger). The
  final boss (L10, not password-reachable) is likely one of the never-witnessed ids.

## To witness the gaps (idx5, idx13–23)
These are in levels/areas the current demos don't cover. The most reliable way to find them is to determine
each level's object-type set from its level-definition data (a future RE step) — then "go to level N". Until
then, recording in unexplored areas of the mid/late levels is the best bet.
