# GOAL: completely finish the renderer island (unattended)

Recover every remaining renderer routine into clean, VM-independent source, proven
byte-exact against the original ASM, with a thin hook surface — until the renderer
island is exhausted. Work on branch `complete-renderer-island`; commit per island;
push the branch (never merge to main — the user reviews and merges).

## Orient first (do not skip)
Read, in order: `docs/pre2/renderer_island.md` (the map, border, gap checklist — the
spine of this goal), `docs/pre2/recovery_architecture.md` (hook roles, the bridge,
verify posture), `docs/pre2/symbol_ledger.md`, and the project memory
(`pre2-renderer-effect-bugs`, `pre2-gog-version`, `pre2-coastline-architecture`,
`pre2-island-composition`, `pre2-isr-crash-freeze`). Then read the existing recovered
renderer modules (`pre2/recovered/{sprite_decode,sprite_classify,renderer,frame_renderer,
object_render}.py`) and their bridges/checkpoints to match style and patterns.

## Invariants (never violate)
- **The lockstep-vs-ASM is the only authority.** If recovered output diverges, the
  recovered code is wrong — fix it, never weaken the verifier. Never trust a guessed
  invariant (it once falsely condemned a correct decoder).
- **No silent fallbacks.** Unrecovered/uncertain behaviour fails loud (`Pre2HybridGap`).
- **`dos_re/` stays game-agnostic.** Pure modules in `pre2/recovered/` have no
  `cpu`/`mem`/`dos_re` imports; layout lives in `pre2/bridge/`.
- **Never regress a verified island.** After every change re-run the verify suite on
  003317, 010021, 003841, 185902, 212037 (all must stay 0 renderer divergences). If any
  regresses, revert that change.
- **Confidence ladder** in `@oracle_link`: GUESS→OBSERVED→RECOVERED→ASM_MATCHED→VERIFIED.
  Only claim VERIFIED with in-VM lockstep over a real run; ASM_MATCHED for committed-
  witness byte-exact. Regenerate the manifest (`scripts/gen_island_manifest.py`); the
  drift test must pass.

## Method per island (the workflow)
1. **Boundary:** find entry + RET with capstone on dumped runtime bytes (the GOG code
   self-unpacks to seg 1030; never trust the VM trace disassembler for Jcc targets).
2. **Faithful witness:** drive a snapshot to where the routine runs with
   `play._pump_and_step` (timer IRQs + SB enabled); capture inputs + VRAM/DAC before and
   after. Reuse the proven techniques: first-diverging-X in draw/call order, per-primitive
   witness at the routine entry→RET, instrumented footprints.
3. **Pure module + bridge:** dataclasses reconstructing the original structs; transform
   with `[asm <off>]` annotations; bridge does layout only.
4. **Verify byte-exact:** committed golden test in `tests/` (small fixtures under
   `tests/fixtures/`) + a verify-mode lockstep hook diffing the contract at the RET.
5. **Thin hook:** replacement adapter at the CS:IP that reads via the bridge, calls the
   pure fn, writes the contract back, returns to original flow; add verify coverage;
   `@oracle_link` metadata; regenerate manifest.
6. Run the full suite + the verify suite on all snapshots; commit on the branch; push.

## Phase 1 — find ALL missing pieces (complete the map)
- Profile (hybrid mode, bucket CS:IP) several snapshots to surface every hot ASM region
  still interpreted: gameplay 185902 / 212037, end-level transition 002633, level-load
  003841, plus 154830/155417/173814/173929/185852/190338. Each hot ASM region is a
  candidate gap.
- For each: capstone-disasm, apply the border test (state→pixels/DAC only, no gameplay
  decision, no data-model ownership), classify renderer-vs-border, record in
  `renderer_island.md`.
- Re-map the scroll engine on GOG: confirm the real addresses of the scroll-source calc
  (ledger `3569`) and the vertical tile-column fill (ledger `34ED`, horizontal-scroll
  counterpart to the recovered row-fill `348D`). The ledger's `3344/338E/33F5` are STALE
  on GOG (that range is the scale transition) — find the true directional-scroll routines
  via the call graph (who calls the fill / the scroll-copy) and record them.
- Build the call graph downward from the frame compositor `3B40` and the tick conductor to
  catch any renderer routine not yet listed.
- Output: `renderer_island.md` gap list is complete and addresses are GOG-confirmed.

## Phase 2 — recover each gap (priority order)
1. **Scale/zoom transition** (`31D0` loop + scaled-column table `31F4–3249` + span-clear
   `32DE` + scaled 4-plane copy `4700`). Reproducible headless from **002633** (it runs in
   the forward-run; profiled at ~63% of transition instructions). Model the per-frame scale
   (`[0x2DD0]` start 0xE6, step `[0x2DC0]`=4) + the span/copy primitives; verify the full
   VRAM result byte-exact across several scale steps.
2. **Scroll engine** (`3569` calc-src + `34ED` column-fill, + the directional-scroll RENDER
   parts — NOT the camera advance, which is border). Verify on a horizontal-scroll snapshot.
3. **Palette fade** (`6772`): linear src(`[0x2D00+[0x2D8A]*2]`)→target(`[0xACB7]`) DAC
   interpolation by `[0x6C03]`/call (48 6-bit components via 3C8/3C9), clears
   `[0x6C01]`/`[0x6C02]` when done; `[0x6C02]` swaps direction. Verify the DAC contract.
4. Any new gaps found in Phase 1.

**Repro-gating (important):** the palette fade and (likely) horizontal scroll do NOT
activate in the static forward-run of the current snapshots — they need a trigger. If, after
honest effort (driving with input scancodes via `deliver_scancode`, trying every snapshot),
a gap cannot be made to run, DO NOT guess an implementation. Record it in `renderer_island.md`
as `NEEDS REPRO: <exact snapshot/trigger needed>` and move on. Recover everything that CAN be
witnessed; leave a precise request for the rest.

## Phase 3 — clean + refactor (behaviour-preserving; verify after each)
- **object_render record-mutation split** (from the renderer review): introduce
  `SpriteRecordUpdate{new_life, drawn}` + `SpritePlan{update, draw|None}`; `plan_sprite`
  returns the plan (update always present, even on cull); the checkpoint APPLIES the update
  via a bridge `write_record` instead of re-deriving the life-decrement inline; bring the
  record mutation into verify-mode coverage (diff life/flags vs ASM, not just planes). Keep
  object_render at 0 divergences.
- **`read_active_list` off-by-one**: iterate `LIST_TOP - RECORD_BYTES` down to `LIST_BASE`
  (the ASM pre-decrements via the jmp from 2719); the current start reads a spurious top
  slot. Verify unchanged behaviour (the top slot is empty today, so it must stay 0-diff).
- **Coastline shortening** (island-composition rule): where a recovered island returns to
  ASM but the callee is a verified recovered fn whose contract covers the side effects, call
  it directly. Grow the recovered↔recovered surface, shrink the ASM boundary.
- **Merge-target taxonomy**: make `@oracle_link` merge targets consistent
  (renderer/frame renderer/sprite pipeline) per `renderer_island.md`.
- **Prune** `pre2/probes/` scaffolding for islands now proven by committed tests + verifier.

## Phase 4 — culminate in `update_frame()`
Once the frame_renderer leaves (grid/scroll-copy/panel), the directional scroll, and the
transitions are recovered, wire the frame compositor `3B40` as a recovered `update_frame()`
that composes the verified leaves directly (recovered→recovered), with verify coverage at
its RET. This collapses the per-hook coastline to one clean frame entry — the architecture
goal. If `3B40` still has no reachable scenario, document that it's recovered-but-unwired and
verify it offline against its static composition.

## Done when
- `renderer_island.md` gap list is fully ticked: each item recovered + VERIFIED/ASM_MATCHED,
  or explicitly `NEEDS REPRO: <…>`.
- Full suite green (the single pre-existing `nuked_opl3` `.pyd` failure is expected — leave it).
- Verify-hooks shows **0 renderer divergences** on every available snapshot.
- Recovered renderer modules are clean VM-independent source; hooks are thin; `update_frame()`
  composes the leaves where a scenario allows.
- Write `docs/pre2/renderer_status.md` summarising what was recovered, what is NEEDS-REPRO,
  the final border, and any follow-ups. Update the project memory.

## Unattended guardrails
- Commit each verified island / refactor as its own commit with a clear message; push the
  branch so progress is reviewable. **Do not merge to main.**
- Run the full suite before each commit; if it regresses, fix or revert before continuing.
- If blocked on one gap, document it and continue with the next — never stall the whole run.
- Keep `dos_re/AI_PORTING_CHARTER.md` + `recovery_architecture.md` invariants throughout.

## Verification snapshots (available under artifacts/)
snapshot_pre2_20260622_{002633 (scale/tally), 003317 (flash-fixed), 010021 (black-fixed),
003841 (level-3-fixed)}, snapshot_pre2_20260621_{185902, 212037 (gameplay), 154830, 155417,
173814, 173929, 185852, 190338}. Use `play._pump_and_step` + `enable_sound_blaster` +
`enable_pre2_hook_verification` to drive + diff (see pre2-renderer-effect-bugs memory for the
exact harness).
