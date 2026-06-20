# dos_re — AI Porting Charter

**Audience:** an AI agent given the `dos_re` package and a DOS game (an MZ/LZEXE
executable + its data files). This document tells you *exactly* what we are
trying to achieve, the method, the tools `dos_re` already gives you, the
invariants you must never break, and the traps that will silently invalidate
your work if you ignore them.

Read this whole file before writing code. This method was first proven on an
earlier DOS-game port; **your game is different** — in this repo the game is
**Prehistorik 2** and the per-game adapter is `pre2/`. Every concrete address,
video mode, and data layout is an *example* that lives in the adapter, not in
`dos_re`.

---

## 0. North star — what "done" means

A **standalone source port**: the game runs as native code (Python here) that
interprets **zero original 8086 instructions in the hot path**. It still loads
the original data files (sprites, levels, sound sequences, palettes) through
native codecs — that is normal and expected. What disappears is *interpreted
code*, not original *data*.

**"Provably equivalent"** does not mean a formal proof (infeasible for a whole
ASM game). It means the strongest practical proof available to us:
deterministic, **frame-and-state-exact** equivalence against the original,
replayed over a demo corpus that exercises the whole game.

Our structural advantage over a normal source port: **we have the exact
original behavior on tap** (the VM) to diff against. The entire method is about
keeping that diff *cheap and total* while we hollow the VM out.

---

## 1. The core idea: turn the VM from the engine into the oracle

Right now the VM does two jobs: it **runs** the game and it **is** the ground
truth. The whole migration is splitting those apart:

- native code progressively takes over **running** the game;
- the VM is demoted to **proving** the native code is correct.

The source of truth shifts over time:
1. early: "does each replacement hook match its original routine at its CS:IP?"
2. later: "does the live game's full frame + decoded state still match the
   original, replayed over recorded input?"

You are always doing one of two things: **lifting** logic out of the VM into
native code, or **strengthening the proof** that the lift was exact.

---

## 2. The two invariants (never break these)

1. **Always runnable.** The game is playable/observable at every commit. There
   is no long-lived "big rewrite" branch. Every change is a thin slice.
2. **Always verified.** Every slice is proven against the VM oracle *before* it
   is trusted. You never replace original behavior with code you have not diffed
   against the original.

If a change cannot be verified, it is not done — it is a hypothesis.

A third law underlies both: **the original executable is the oracle. Never
guess.** If you don't know what a routine does, you trace it in the VM and read
what it actually did; you do not invent plausible behavior.

---

## 3. Architecture: reusable core vs per-game adapter

### 3.1 `dos_re/` — the reusable, game-agnostic core

`dos_re` knows about the 8086, DOS, memory, snapshots, input, and the two
verification engines. It knows **nothing** about any specific game's addresses,
video layout, or data formats. Key modules:

- **`cpu.py` — `CPU8086`.** The interpreter. Important surface:
  - `step()` runs one instruction (or one replacement hook); `run(n)` runs n.
  - `replacement_hooks: dict[(cs,ip) -> handler]` — install native code at an
    address. `hook_names` parallels it for reporting.
  - `hook_verifier` — if set, `step()` routes a hooked address through it
    instead of calling the handler directly (this is how verification wraps
    every hook). `hook_verifier_passthrough` exempts addresses (e.g. UI pacing
    wrappers) from being verified.
  - `coverage_telemetry` — records interpreted vs hooked instruction counts and
    per-hook ASM-equivalent cost (your headline progress metric, see §10).
  - `trace_enabled` / `trace` — per-instruction disassembly+state capture.
  - `instruction_count`, `addr() -> (cs,ip)`.
- **`memory.py` — `Memory`.** Flat image + segment helpers (`rb/rw/wb/ww`,
  `block`, `linear`), plus EGA planar aperture support.
- **`dos.py` — `DOSMachine`.** INT 21h/16h/etc., `key_queue`, file handles,
  speaker/AdLib callbacks.
- **`runtime.py` — `Runtime`.** Bundles `cpu`, `dos`, and the loaded program
  image (`program.memory`). The unit you clone for an oracle.
- **`mz.py`.** MZ executable loader (relocations, PSP, segment layout). Handle
  packed loaders (LZEXE etc.) as a bootstrap that runs once to materialize the
  real image, then snapshot past it (see §4).
- **`snapshot.py` — `write_snapshot` / load.** Freeze/restore full machine
  state. Snapshots are how you skip the slow asset-decode bootstrap and how you
  pin a reproducible starting point for verification.
- **`interrupts.py` — `deliver_scancode`.** Inject a hardware-style key event
  (port 60h + the game's INT 09h ISR), not BIOS INT 16h — most action games
  poll their own ISR/key-state table.
- **`keyboard.py` — `KeyDispatcher`.** Holds each key down for ≥1 full polled
  frame before releasing (same-frame make+break taps are lost otherwise).
- **`input_demo.py` — `InputDemoRecorder` / `InputDemoPlayback`.** Record a
  start snapshot + VM-visible key events keyed to an **emulated boundary
  counter**; replay them deterministically into one or more runtimes. This is
  the substrate of the proof corpus. **Read §6 before trusting it.**
- **`verification.py` — the hook oracle.** `HookVerifier`,
  `HookVerifierConfig` (incl. `.strict()` = auto-continuation mode),
  `install_hook_verifier`, `HookVerifyDivergence`, `HookVerifyLimitReached`,
  and the `StopRule`/`HookStop` "continuation metadata" system. Two oracle
  modes:
  - **metadata mode:** you declare each hook's valid continuation target(s)
    (near-ret, far-ret, iret, fixed-ip, computed dispatch, …); the verifier
    clones the runtime, runs the *original* ASM to that target, runs your hook,
    and diffs registers + flags + full memory.
  - **auto-continuation (`strict`) mode:** runs your hook first, takes its final
    address as the only acceptable target, then runs the original ASM to that
    same address and diffs. No metadata to maintain; slower; ideal for focused
    investigation.
  - **`OK_TRACE_HOOK="CS:IP"`** (env): on a divergence at that hook, prints the
    exact ASM-oracle instruction trace. This is the primary debugging tool —
    it shows precisely what the original did that your hook did not.
- **`frame_verify.py` — the semantic/frame oracle.** `run_frame_verifier`
  steps two runtimes (reference = original ASM oracle, candidate = hooked/native)
  to caller-defined **frame boundaries**, builds a `FrameSample` at each, and
  diffs them (`compare_samples`, `dump_divergence` writes PNG/VRAM/report
  artifacts). Game-independent; the adapter supplies boundary addresses, a
  `sample_builder`, `reference_env_hooks`, optional `pump_inputs`, and an
  `input_wait_detector` (see §6).

### 3.2 The per-game adapter (`pre2/` in this repo) — everything game-specific

This is what *you* write/extend for your game. It contains the only code that
knows your game's addresses and formats:

- **Replacement hooks** (`hooks.py` / `replacements.py`): native handlers
  registered to original CS:IP addresses, typically via a
  `@registry.replace(cs, ip, name)` decorator that auto-installs them.
- **Continuation metadata** (`verification.py: DEFAULT_STOPS`): one `HookStop`
  per hooked address describing its valid return/continuation.
- **Frame-verify adapter** (`frame_verify.py`): your game's present/timer/retrace
  boundary addresses, video-memory layout, palette/renderer, and the
  `reference_env_hooks` (hardware-wait hooks the *oracle* must keep so it does
  not spin forever — typically the PIT/timer wait and the CRT retrace wait).
- **Input-wait registry** (`input_waits.py`): detectors for busy-wait input
  loops that produce no frame boundary (see §6 — this is mandatory, not
  optional).
- **Runtime loader** (`runtime.py`): build/snapshot-load the runtime with the
  right command tail (video/sound selection) and assets.
- **Asset codecs** (`asset_codecs/`, `file_io/`): native decoders for the game's
  packed data.
- **Coverage classifier**: maps addresses to "islands" (subsystems) so progress
  telemetry is meaningful.

**Rule:** if a piece of code mentions a concrete address, video mode, or file
format, it belongs in the adapter, never in `dos_re`.

---

## 4. The disciplined lifting loop (do this for every slice)

This is the unit of work. One routine, one verification, per slice.

1. **Run the original** under the VM; reach the state you want to study (load a
   snapshot to skip the bootstrap).
2. **Trace** to identify exactly ONE routine to lift. Use `trace_enabled` or the
   linear disassembler.
3. **Snapshot** before/after the routine so you have a reproducible fixture.
4. **Write a narrow native hook** at the routine's CS:IP — a *thin VM adapter*
   (reads/writes VM memory and registers) wrapping a **pure recovered rule**
   (the actual game logic, side-effect-free where possible, unit-testable).
5. **Declare continuation metadata** (`HookStop`) for the address, or use strict
   auto-continuation mode.
6. **Verify against the interpreted ASM oracle**: a test in the adapter's test
   suite asserts `snapshot()` + memory blocks equal between your hook and the
   original ASM executed from the same pre-state. The oracle is the interpreter
   executing the real bytes — never a hand-written expectation.
7. **Update** the symbol map, runtime-findings notes, and status doc.

When a hook diverges, set `OK_TRACE_HOOK` to that address, reproduce, and read
the ASM oracle trace. Fix the hook to match what the original *did*, not what
you think it should do. (Real examples from the reference port: a hook took an
"empty-scan" exit where the original jumped straight to a shared RET, leaving a
register/flag wrong; another modeled a nested call in Python without leaving the
call's return-address word on the freed stack, which full-memory verification
caught. You will hit exactly this class of bug — freed-stack scratch, flag
shape, early-out branch selection. The trace tells you which.)

---

## 5. The proof spine (build it ahead of the lifting)

Verification must get **stronger as the VM gets weaker**, because collapsing
code loses per-hook granularity. Evolve it in this order:

1. **Per-hook ASM match** (have now): every hooked address diffed register +
   flag + full-memory against the original at its continuation.
2. **Semantic frame verifier** (have now, via `--verify-frames`): diff
   ASM-oracle vs candidate at each frame boundary — framebuffer + visible VRAM
   to start.
3. **Widen the semantic snapshot** until it covers **all observable state**:
   every object field, player, **RNG state**, score/lives, level/wave state,
   timers, and the framebuffer. *If it is not in the snapshot, divergence can
   hide there.* Locating the RNG state in memory is usually the first hard
   sub-task and a prerequisite for everything downstream.
4. **Deterministic demo-replay harness**: for each recorded demo, assert
   candidate ≡ oracle for **every frame to the end**. Determinism (fixed input +
   seed ⇒ identical state) becomes a hard requirement; any wall-clock or RNG
   nondeterminism must be modeled out in verify mode.
5. **Demo corpus** covering all levels, bosses, spawn types, edge interactions,
   and RNG paths. "Proven equivalent" = every demo passes full-frame/full-state,
   and you track which behaviors/branches the corpus exercises so confidence is
   *measured*, not vibed.

---

## 6. Determinism and the boundary-clock invariant (the trap that voids proofs)

**This is the most important non-obvious section. Read it twice.**

Demo events are keyed to an **emulated boundary counter** ("the demo clock").
A demo is only a valid proof artifact if it is **byte-for-byte reproducible
across every driver** that replays it. There is typically more than one driver:

- an interactive play loop,
- a headless per-hook verifier,
- the frame verifier (`run_frame_verifier`).

If these count "a boundary" differently, the same demo replays at different
internal points in each driver, gameplay diverges, and your corpus pass/fail
becomes driver-dependent — i.e. the proof is an illusion. In the reference port
this manifested as **freezes/deadlocks**, not loud errors, which is worse.

Two concrete failure modes you *will* hit:

1. **Boundary-less input-wait loops.** Some original code busy-waits on the
   keyboard *without* reaching a timer/retrace/present boundary (e.g. a "press
   FIRE to start" / "wait for FIRE release" poll). The demo clock is frozen
   inside such a loop, so a recorded key *release* keyed to a later boundary is
   never delivered — the loop waits forever for input it can't receive. **Every
   driver must recognize these loops** and treat them as a boundary so input is
   pumped and the clock advances. Keep these detectors in **one shared registry**
   (the adapter's `input_waits.py`) consumed by all drivers — duplicating them
   per-driver guarantees they drift. In the frame verifier, detect the loop at
   its **canonical head address** and check it **every step**, so the reference
   and candidate stop at the *identical* instruction; if they stop at different
   sub-positions of the loop they resume differently when input is pumped and
   diverge spuriously.

2. **Driver-specific clocks.** Before standing up the demo-replay corpus,
   **unify the boundary/clock definition** so record-time and replay-time, and
   every driver, agree on exactly what increments the counter. This is a
   prerequisite for step 4 of the proof spine, not a cleanup afterward.

Also model out, in verify mode: real-time pacing (no wall-clock sleeps gating
state), asynchronous timer-IRQ delivery (make it deterministic), and RNG seeding.
The oracle must keep the *hardware-wait* hooks (timer, retrace) so the original
ASM doesn't spin on a flag that, in real hardware, an interrupt would clear —
but those waits must return deterministically.

---

## 7. Phased roadmap

- **Phase 1 — Lift every game rule out of hook bodies.** Turn each gameplay
  hook into a thin VM adapter over a pure, tested recovered rule (object
  behaviors, collision predicates, movement/clamp, spawn selection,
  contact/overlap, HUD/state). Exit: decisions are native even though the VM
  still owns memory and the loop. Proof: per-hook ASM match + frame verifier.
- **Phase 2 — Collapse understood hook chains.** Where the runtime ping-pongs
  ASM→hookA→glue→hookB→ASM and the glue is understood, fuse into one native
  flow. Accept coarser hook-coverage granularity; the frame/state verifier
  covers it. Exit: gameplay control flow is native, not CS:IP ping-pong.
- **Phase 3 — Decode all game data into native structures.** Jump tables,
  sprite/animation tables, level scripts, sound sequences, palettes — decode via
  typed decoders into native data the lifted rules consume. Exit: a native data
  layer loadable from the original files without a running VM. Proof: round-trip
  (decode → re-encode → byte-identical to the original image) + rules behave
  identically on decoded vs VM-read data.
- **Phase 4 — Earn the native world model + systems.** Only now compose the
  accumulated rules into systems over a native world model (the abstraction is
  *earned* by Phases 1–3, not invented up front). Run as a shadow: decode VM
  state, advance natively, semantic-diff every frame. Exit: a native `tick()`
  reproducing the VM frame for the systems it owns.
- **Phase 5 — Native backends.** Wire Video/Input/Audio/Timing/AssetProvider to
  real adapters (framebuffer producer, speaker/FM synthesis, keyboard→action,
  deterministic timing). Exit: native systems produce frames/audio without the
  VM's render/sound hooks. Proof: native framebuffer == VM framebuffer; audio
  register/tone streams match.
- **Phase 6 — Flip the engine, keep the VM as oracle.** Native loop drives; VM
  runs only in test/dev as the proof harness; ASM interpretation leaves the hot
  path. Exit: standalone playable build. Proof: the full demo-corpus equivalence
  suite passes native-vs-VM end-to-end.

Never break "always runnable, always verified" between phases.

---

## 8. Cross-cutting hard parts (call these out early for your game)

- **The music/sound driver** (often a separate resident segment, millions of
  interpreted instructions, a timer-ISR-driven sequencer). "Exact audio" means
  matching its synth-chip register stream. Usually the longest pole; treat as a
  separable chunk.
- **The main frame loop / dispatcher.** Collapsing it to a native loop is the
  riskiest step — leave it for Phase 6.
- **Self-modifying / runtime-patched routines.** Static lifting must handle the
  *patched* variants; the verifier must catch them (full-memory diff helps).
- **Determinism.** RNG + timing must be exactly reproducible in verify mode or
  the proof spine collapses (see §6).
- **Rendering.** Pick one primary video mode; keep the others isolated and not
  allowed to block the primary.
- **Packed executables (LZEXE etc.).** The bootstrap unpacker is dynamic-init
  materialization, not game logic — run it once, snapshot past it, lift from the
  unpacked image.

---

## 9. Bootstrapping a NEW game on `dos_re` (concrete checklist)

1. **Load & run.** Get the MZ image loading and the unpacker (if any) running;
   reach the first visible frame. Add a snapshot point after init.
2. **See output.** Decode the active video memory to an image so you can
   visually confirm state. Wire keyboard input via `deliver_scancode` and verify
   the game's key-state table updates.
3. **Find the frame boundaries.** Identify the timer/PIT wait, the CRT retrace
   wait, and the present/blit routine(s). These become your frame-verify
   boundary hooks and `reference_env_hooks`.
4. **Stand up the frame verifier** (adapter `frame_verify.py`): boundaries +
   `sample_builder` (framebuffer + visible VRAM first). Confirm a no-op
   candidate (no hooks) matches the oracle frame-for-frame.
5. **Build the input-wait registry** (`input_waits.py`) — find the boundary-less
   poll loops (title/menu/“press fire”) before recording any demo (§6).
6. **Record a first demo** that drives menus into gameplay; confirm it replays
   identically under every driver.
7. **Start Phase 1** on the densest gameplay routines, one slice + one
   verification each, following §4.
8. **Stand up coverage telemetry** so you can report progress (§10).

---

## 10. Progress metrics (replace "hook coverage" as the headline)

- **% of per-frame VM instructions running through native code vs interpreted
  ASM** (from `coverage_telemetry`). This is the headline number.
- **# of gameplay rules lifted to pure recovered functions, with tests.**
- **Semantic-snapshot state coverage** (fraction of observable state decoded +
  diffed).
- **Demo-corpus coverage** (levels/behaviors/RNG paths exercised) and **pass
  rate**.

**Definition of done:** the native loop runs the whole demo corpus with the VM
disabled in the hot path, and the VM-as-oracle suite confirms frame-and-state
exact equivalence for every demo in the corpus.

---

## 11. Rules of engagement (the laws)

1. **The original executable is the oracle. Never guess** a routine's behavior —
   trace it and read what it did.
2. **Verify before you trust.** No lift is "done" until diffed against the ASM
   oracle (per-hook) and/or the frame/state oracle (demos).
3. **Thin slices only.** One routine or one understood chain at a time; the game
   stays runnable at every step.
4. **Pure rule + thin adapter.** Keep recovered game logic side-effect-free and
   unit-tested; isolate VM memory/register I/O in the adapter.
5. **Game specifics never leak into `dos_re`.** Addresses, video modes, and
   formats live only in the adapter.
6. **One shared definition of "a boundary" and "a wait loop."** All drivers must
   agree, or the demo proof is void (§6).
7. **Full-memory + full-state diffs by default.** Narrowing the diff hides bugs
   (freed-stack scratch, flags, off-screen state). Narrow only as a deliberate,
   temporary performance lever.
8. **When stuck, get the trace.** `OK_TRACE_HOOK` (per-hook) and the frame-verify
   divergence artifacts (semantic) tell you exactly where reality and your model
   parted. Read them before theorizing.

---

*End of charter. The method here was first demonstrated on an earlier DOS-game
port; reuse the patterns, but re-derive every address and format for Prehistorik
2 — and re-verify everything against `assets/pre2.exe`, which is your only
oracle.*
