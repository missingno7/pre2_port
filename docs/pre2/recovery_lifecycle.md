# DOS Game Recovery Lifecycle (north-star companion)

This is the canonical articulation of *what we are building and why*, the companion to
`recovery_architecture.md` (which is the PRE2-specific posture). Where they agree, that is
intentional. The endgame is **not** a VM with a nicer renderer — it is a **complete recovered
native source port, startable from a cold start like a normal game, with the VM kept only as an
oracle**. We are reconstructing the original game's source.

```text
original DOS game
    -> boot in VM            (the oracle: runs original ASM, traceable, verifiable)
    -> hook hot routines     (scaffolding — temporary contact points, not the architecture)
    -> recover verified islands  (permanent high-level source, byte-exact vs the oracle)
    -> merge islands -> recovered core
    -> lift into a VM-less faithful runtime  (NativeGameState owns state + tick)
    -> extract standalone faithful product   (assets + recovered core + modern platform layer)
    -> keep the VM as optional oracle
```

## The destination, stated without confusion

- **Full source-code reconstruction.** Clean, readable high-level code resembling the original
  (almost certainly C) source — not a remake, not an editor-runtime. Hooks are scaffolding;
  reconstructed structs + recovered functions are the product. *Concretely:* gameplay logic reaches
  state through a human-named **view layer** (`s.wind`, `slot.x`), with the original DGROUP offsets
  confined to one layout module — see [`state_view_layer.md`](state_view_layer.md). The runtime state
  stays a byte-image *internally* (the faithful representation and the verification surface are the same
  bytes); **byte-backed ≠ VM-backed** — it is pure Python data, no EXE and no emulator. "Offsets out of
  the *logic*" is the milestone; removing the offset map from the *release binary* is explicitly a
  non-goal (it is not the EXE, not a VM, and not a silent fallback).
- **Cold-start, standalone.** The finished product boots from the **game data files alone** —
  intro → title → menu → world map → level → gameplay → death/respawn → game-over → menu — exactly
  like the original, with **no EXE and no snapshot** at runtime. (Snapshots/demos are recovery
  *evidence*, not a runtime dependency.)
- **Oracle-verified.** A verification switch: ON → the VM oracle runs beside the native game and
  diffs state/render/audio at boundaries; OFF → no VM starts, the native game runs by itself.

## Equivalence boundaries — the contract per subsystem (Phase 9)

The VM preserves the original **machine**; the faithful port preserves the original **game**.
Different subsystems have different equivalence rules:

- **Gameplay simulation — STRICT / byte-exact.** Player, objects, enemy AI, collisions, physics,
  damage, pickups, score/lives, **RNG**, timers, scene/level progression, input-as-seen-by-the-tick.
  Contract: *same initial state + same input history + same tick → same game state.* Close enough is
  not faithful. (This is why death/respawn/combat must be recovered against demo witnesses, not guessed.)
- **Rendering — pixel-exact, mechanism-flexible.** The native renderer need not reproduce EGA
  bitplanes / VGA latches / write-modes / A000h tricks. Contract: *same recovered render state →
  same visible pixels + palette at the same frame boundary.*
- **Audio — event/timing-exact, mixer-flexible.** Which music/sfx, when, priority/order, the causing
  gameplay event. The mixer may be modern; it must not feed back into gameplay.
- **Input — semantic/timing-exact, hardware-path-flexible.** The hardware path may differ; the
  game-visible input state at tick boundaries must match.
- **Timing — same heartbeat, NOT same waiting machinery.** The native port must **not** reproduce
  busy-waits / vertical-retrace polling / host-speed delay loops. It preserves the **game tick
  cadence** (update/animation/physics/scene/audio cadence), not the DOS waiting machinery.

## The heartbeat (Phase 10) — why "the same logical tick," not the same spin

Removing DOS timing machinery is what creates the verification problem *and* the speed question.
The native port exposes the recovered game-visible heartbeat explicitly
(`GameTick`/`FrameBoundary`/`InputSampleBoundary`/`RenderBoundary`/`AudioEventBoundary`/
`SceneTransitionBoundary`) and advances a **fixed-step** simulation at the original cadence. It does
not emulate the retrace spin; it matches *what the game becomes at each heartbeat*. The standalone
runner must therefore pace gameplay to the recovered tick rate — running "as fast as the display"
is a bug, not faithfulness.

## Verification at boundaries + state mirrors (Phases 11–12)

Compare native ⇄ oracle at stable boundaries (tick/frame/input/render/audio/scene) via projection
layers (`NativeGameState ⇄ VM oracle checkpoint`). The mirror **verifies** faithful; it does not
**power** faithful — verification OFF must run the native game with no VM and no projections.

## Where PRE2 is on this lifecycle (2026-06-30)

- Phases 1–7 (transplant → hooks → islands → merge): **done.** The whole faithful gameplay frame's
  *prefix* (581E..5643) is byte-exact over demos; renderer + player + interaction + camera islands live.
- Phase 8–13 (lift to VM-less core): **in progress.** `NativeGameState` + `native_gameplay_frame`
  + `native_render` + `native_level_load` exist; a level is playable VM-less. GAPS: the rest of the
  main loop past `0x3668` (firefly/shake to wire; death/respawn + level-state machine + scripted-scroll
  to recover — **verified against the death/game-over demos**, e.g. the 19380-frame run); the
  **cold-start boot** (front-end flow + full level-init, no snapshot); the explicit **heartbeat/pacing**.
- Phases 14–16 (presentation/standalone/oracle): the enhanced layer + verification switch exist in part.

**Mantra:** the VM preserves the original machine; the faithful source port preserves the original
game. Hybrid prepares the code; faithful plugs it in; the oracle proves it did not drift.
