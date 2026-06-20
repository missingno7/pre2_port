# pre2/checkpoints/ — verification contact points

Verifiers/checkpoints that compare recovered logic against the original ASM oracle
at contract boundaries. **Scaffolding, not the architecture** — proof tools, not
where game logic lives.

A checkpoint:
- reads original state (and/or the recovered dataclass via `pre2/bridge/`),
- runs the recovered logic and the ASM oracle from the same inputs,
- diffs the **contract** (early: raw memory/register/buffer/framebuffer; later:
  semantic state — `PlayerState`/`ObjectState`/…/`GameState`, frame/tick), and
- reports the first divergence with enough state to localise it.

Each checkpoint has a declared role and lifetime; as islands merge, checkpoints
move **up** to cleaner boundaries and become fewer.

Today the SQZ verifier lives in `pre2/replacements.py`
(`enable_pre2_hook_verification`, driven by `play.py --verify-hooks`); verifiers
move here as they grow beyond a single island. See
`docs/pre2/recovery_architecture.md`.
