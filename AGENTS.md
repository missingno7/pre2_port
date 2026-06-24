# AGENTS.md — Prehistorik 2 evidence-driven source-port project

These instructions apply to the whole repository. They are written for AI agents
and humans working on the Prehistorik 2 runtime/source-port project.

## Project purpose

Build a narrow, evidence-driven runtime and source port for one specific 16-bit
DOS game: **Prehistorik 2**. The reusable `dos_re` real-mode VM is the execution
oracle; everything Prehistorik 2-specific lives under `pre2/`.

This is not a general DOS emulator and must not drift into one, and it is not a
loose remake. The original `assets/pre2.exe` remains the behavioural oracle. The
long-term shape is a hybrid source port:

1. Run the original DOS code in the custom 8086 runtime.
2. Trace real control flow, memory, registers, files, ports, and interrupts.
3. Understand one bounded routine or subsystem at a time.
4. Replace only proven behaviour with native code.
5. Verify each replacement against interpreted original ASM.
6. Move stable replacements into readable game-specific modules.
7. Keep the original binary as the oracle until the source port can stand alone.

## Working principles

Correctness beats speed. Traceability beats cleverness. Small verified progress
beats large intuitive rewrites.

The original executable is the only oracle. Do not infer behaviour from other DOS
games. Do not replace a system by intuition: if a routine is not understood,
trace it, snapshot it, document it, and replace the smallest coherent unit whose
boundary is proven. A faster wrong replacement is a regression.

**Do not write high-level gameplay because it "looks right". Write high-level
logic only when it can be tied back to original ASM behaviour and verified.**

## Where the project is now (recovery phase)

Bootstrap is done. The VM **runs PRE2 gameplay**, and recovered native code is now
part of the normal **hybrid** runtime. The first recovered-native island — **SQZ
asset decompression** (`pre2/codecs/sqz.py`) — is complete and verified
byte-for-byte against the ASM (LZSS, LZW, Huffman+RLE "other"); the hybrid runtime
cold-boots into gameplay decoding every asset natively.

The work is now recovering more **verified, bounded, hot kernels** and moving them
upward into clean source-like modules. Good next targets: LZW/SQZ variants (done),
**sprite/tile decode, masked blits, tilemap/background draw**, then gameplay systems
(player/object/level update). Each island is clean VM-independent logic behind a
thin adapter, verified before it is trusted.

### Three execution modes (no silent fallbacks)

- **oracle / original** — pure original ASM (`create_pre2_runtime(..., native_replacements=False)`); reference and observation.
- **hybrid (default)** — recovered native replacements run directly, no per-step verification; the active runtime (`play.py --view`).
- **verify** — ASM oracle vs recovered logic, diffed at contract boundaries (`play.py --verify-hooks`); for offline proof against demos/snapshots.

The original ASM runs **only** in oracle/verify modes. In hybrid mode, unrecovered
behaviour **fails loud** (`Pre2HybridGap`) — never a silent fallback to ASM.

### Hook/checkpoint roles (every contact point has one + a lifetime)

- **probe** — observe the original ASM (tracing, capturing oracles);
- **verifier / checkpoint** — compare a recovered island against the original;
- **replacement adapter** — replace a known ASM path in the hybrid runtime;
- **temporary gap detector** — fail loud on unrecovered behaviour.

A hook is scaffolding, not the architecture, and never where game logic
accumulates. See [`docs/pre2/recovery_architecture.md`](docs/pre2/recovery_architecture.md)
for the full posture and the memory-view ↔ dataclass bridge model.

### North star — the hybrid recovered-source runtime (the convergence model)

We are building a **hybrid recovered-source runtime**: the original `assets/pre2.exe`
stays the behavioural **oracle**, while expensive/important ASM rendering/audio/timing
routines are progressively **replaced by verified high-level recovered code**. The end
goal is recovered high-level source that **both the hybrid runtime and the faithful video
backend use**. Every visual/audio/timing behaviour converges along ONE chain:

```text
original ASM producer
  -> hook / checkpoint / probe              (discover + verify the REAL routine)
  -> verified recovered high-level source   (pure fn in pre2/recovered/ or pre2/codecs/)
  -> live replacement in the hybrid runtime (where the contract is stable/safe)
  -> FaithfulVisual consumes the SAME recovered source (a faithful video backend)
  -> later: enhanced renderer/audio backend consumes the same semantic model
```

**Why hook-first (practical + architectural):** the VM interpreter is too expensive if
every draw/audio/timing routine stays interpreted ASM; live replacement moves the hot
behaviour into recovered code; the verifier proves it matches the oracle; that same
recovered code is then the foundation FaithfulVisual composes. **Hooks are the *roots* of
recovery, not the final shape.**

**Convergence is bidirectional:**
- *bottom-up* — hooks/checkpoints discover + verify real original behaviour and lift it to a leaf.
- *top-down* — FaithfulVisual may reveal a missing visual behaviour, but that behaviour must be
  pushed back DOWN into a hook/checkpoint/recovered leaf (grounded against the oracle) before it is
  canonical. FaithfulVisual **never invents behaviour** and **never reads the VM framebuffer** — an
  unrecovered piece is a LOUD gap, never an ASM-VRAM fallback.

**Two wrong extremes — the docs and your work must make BOTH hard to fall into:**
- ✗ build FaithfulVisual first by guessing visual intent from screenshots, ground it "later";
- ✗ accumulate a permanent pile of tiny isolated hooks with no recovered-source structure.
- ✓ **correct:** hook/checkpoint the real producers → extract verified recovered source → replace
  ASM where safe → FaithfulVisual composes the SAME recovered source → collapse into larger islands
  only when the real original structure supports it.

> One sentence to remember: *FaithfulVisual is a faithful video backend over recovered source;
> recovered source is grounded by hooks/checkpoints against the original runtime; hook-first does
> not mean hook-pile-forever, and faithful-first does not mean inventing behaviour.*

### One recovered leaf, many adapters

The **recovered leaf** (a pure fn in `pre2/recovered/`) is the primary artifact; each leaf has
thin **adapters over the ONE implementation**, never a second copy: **(1) live replacement** (the
hybrid runtime skips the ASM body), **(2) verify checkpoint** (diff vs the oracle at the boundary),
**(3) FaithfulVisual consumer** (composes the same leaf), **(4) later enhanced backend**. Order:
ground the live hook + verifier FIRST; FaithfulVisual absorbs the grounded leaf LAST. Full detail:
[`docs/pre2/faithful_visual_layer.md`](docs/pre2/faithful_visual_layer.md).

### Status taxonomy — every rendering/audio piece is exactly one of these

1. **recovered + live-grounded** — recovered leaf + live replacement hook + verifier (runs in hybrid).
2. **recovered, verify-only** — recovered leaf + checkpoint diff, but the ASM still draws (no live skip).
3. **faithful-only diagnostic/capture** — consumed by FaithfulVisual but NOT yet grounded by a live hook
   (a transitional state to fix — NOT an endpoint, NOT "done").
4. **known gap** — not recovered; FaithfulVisual fails loud here (no VM fallback).
5. **blocked — history-dependent buffer state** — the real game keeps stateful VRAM (a circular
   scroll-page ring, a `scroll_shift` self-copy, …); a from-scratch rebuild is WRONG. Needs the real
   stateful model / replay, not a guess.
6. **not worth hooking** — a pure controller / setup / present wrapper with no hot or reusable behaviour.

### Collapse rule

Collapsing several hook leaves into one larger recovered island/controller is desirable — but ONLY with
**evidence from the real original call graph** (the leaves genuinely belong to one original
routine/controller/compositor). **Never** collapse to a modern invented design.

## Sources of truth

- [`docs/pre2/recovery_architecture.md`](docs/pre2/recovery_architecture.md): the
  north-star posture — hook roles/lifetimes, island merge targets, the
  memory-view ↔ dataclass bridge, contract verification, the two/three modes.
- [`docs/pre2/run_status.md`](docs/pre2/run_status.md): current phase, recent
  fixes, proof artifacts.
- [`docs/pre2/source_port_plan.md`](docs/pre2/source_port_plan.md): boundary
  rules, phase status, recovered-island roadmap.
- [`docs/pre2/symbol_ledger.md`](docs/pre2/symbol_ledger.md): original addresses,
  continuation points, allocator state, decode boundaries (candidate → verified).
- [`dos_re/AI_PORTING_CHARTER.md`](dos_re/AI_PORTING_CHARTER.md): the full
  reusable porting method — proof spine, determinism trap, phased roadmap.
- [`docs/dos_re/source_port_methodology.md`](docs/dos_re/source_port_methodology.md):
  naming/altitude discipline, evidence ladder, status ladder, hook lifecycle.
- `tests/`: executable proof for CPU behaviour and replacement equivalence.
- `artifacts/`: evidence snapshots and traces used by tests or findings.

Keep durable policy here in `AGENTS.md`. Keep time-sensitive status in
`docs/pre2/run_status.md`.

## Repository layout

```text
dos_re/                 reusable, game-independent DOS RE environment
  cpu.py                dependency-free 8086 interpreter core
  memory.py             20-bit real-mode memory model (+ EGA planar aperture)
  mz.py                 MZ EXE parser/loader helpers
  dos.py                narrow DOS/BIOS/port services
  hooks.py              generic replacement-hook registry
  interrupts.py         generic interrupt/scancode delivery helpers
  keyboard.py           host input -> emulated keyboard state
  runtime.py            generic DOS-program runtime wiring
  snapshot.py           generic memory/state snapshot helpers
  verification.py       reusable differential hook-verifier engine
  frame_verify.py       reusable frame comparison/diff artifact engine
  bootstrap_lzexe.py    target-neutral LZEXE 0.91 loop accelerator
  AI_PORTING_CHARTER.md the reusable porting charter

pre2/                   Prehistorik 2-specific recovery layer
  runtime.py            PRE2 launch/snapshot wiring; installs hybrid replacements
  replacements.py       active replacement adapters (thin hooks) + verify wiring
  bootstrap_hooks.py    bootstrap helpers only (LZEXE/AdLib), no gameplay
  codecs/               recovered VM-independent asset codecs (sqz.py)
  recovered/            recovered VM-independent gameplay logic   [for next islands]
  bridge/               memory views: VM memory <-> recovered dataclasses [stateful]
  checkpoints/          verification contact points               [grows from replacements]
  probes/               temporary observation/diagnostic tools    [as needed]
  launch.py / cli.py    PRE2 entry points
  analysis.py           PRE2 inspection helpers

nuked_opl3/             vendored optional Nuked-OPL3 OPL/AdLib backend
docs/                   methodology (dos_re), architecture, and PRE2 findings
scripts/                runners and RE helpers (play.py, render_frame.py, ...)
assets/                 original Prehistorik 2 files (pre2.exe, *.sqz, *.trk)
artifacts/              generated snapshots, traces, and proof captures
tests/                  DOS runtime and PRE2 regression tests
```

## Architecture rule

`dos_re/` must stay game-independent. Anything that knows Prehistorik 2
filenames, executable layout, bootstrap policy, addresses, or gameplay belongs
under `pre2/`. The boundary is documented in
[`docs/architecture/package_boundary.md`](docs/architecture/package_boundary.md).

The intended migration path:

```text
original PRE2.EXE -> dos_re VM -> bootstrap/source snapshots
  -> PRE2-specific typed views over original memory -> verified hooks
  -> semantic source-port systems
```

## Replacement hook rules

A hook is a **minimal boundary adapter, not a place where logic accumulates**. A
good hook only:

1. reads the relevant state from original memory/registers,
2. calls a clean native recovered function (which knows nothing about CPU,
   segment:offset, pygame, or random memory pokes),
3. writes the result back to original memory/registers,
4. returns to the original control flow.

The recovered logic lives **outside** the VM layer: clean, deterministic, numeric
game logic with explicit inputs and outputs.

Before adding or changing a hook:

1. Identify the exact original entry address, e.g. `1996:01A0`.
2. Confirm the boundary type: near/far routine, loop body, tail-jump target,
   dispatch stub, or parent block.
3. Understand entry state, exit IP, stack, flags, registers, segment registers,
   memory writes, file offsets, and DOS/BIOS/port effects.
4. Produce an oracle by running the interpreted original ASM.
5. Implement a thin wrapper that delegates to a pure recovered function.
6. Add an oracle/regression test in `tests/`.
7. Update the PRE2 address ledger and `docs/pre2/run_status.md`.
8. Run the test suite.

Hook return mechanics must match the original boundary exactly:

```text
near routine:    cpu.s.ip = cpu.pop()
far routine:     cpu.s.ip = cpu.pop(); cpu.s.cs = cpu.pop()
internal block:  cpu.s.ip = <exact continuation IP>
```

Do not assume a routine returns; some are loop bodies, jump targets, or dispatch
stubs. Never add a hook because it looks right — every hook needs oracle evidence.

## CPU / DOS / BIOS / port rules

`cpu.py` is a narrow 8086 interpreter; `dos.py` is a narrow deterministic DOS
model. Add only what PRE2 actually exercises. When the runtime hits an
unsupported opcode or call: decode the exact instruction/addressing mode,
implement only the required behaviour, match flags for the observed use, add a
focused test in `tests/test_core.py`, and avoid broad 80186/286/386 behaviour
unless the executable proves it is needed.

Be careful with: `LOOP` count wrap (`CX=0` ⇒ 65536), rotate/shift flags, `REP`
segment wrapping and string-op source overrides, `LES`/`LDS`, far calls/returns,
and undefined flags the game observes. Document the exact call site and observed
register contract for every new DOS/BIOS/port behaviour. Keep file IO handle
offsets exact.

## Snapshot, artifact, and verification rules

Snapshots are evidence — name them descriptively
(`artifacts/<purpose>/`, typically `memory_1mb.bin` + `state.json` +
`trace_tail.txt`). Keep artifacts that justify hooks, tests, or findings; do not
delete evidence snapshots just because they are large unless asked. Scratch
traces not referenced by a test or doc may be pruned.

Verify replacements against the interpreted ASM oracle: compare as much as the
boundary observes — GP/segment registers, `CS:IP`, flags, `SS:SP` scratch,
touched memory, DOS handle/file offsets, port state, and video memory/frames for
visual paths. Prefer synthetic fixtures for small routines and captured snapshots
for larger paths.

## Standard commands

`play.py --view` runs the **hybrid runtime** (recovered native replacements run in
place of the ASM). Add `--verify-hooks` for verify mode (lockstep ASM oracle
check). Rendering covers BIOS text, linear VGA, and the 320x200 16-colour planar
path; audio is the vendored Nuked-OPL3 backend driven by the original AdLib stream.

```bash
python scripts/run_tests.py                                   # test suite
python scripts/play.py --inventory                            # inspect original files
python scripts/play.py --view                                 # hybrid runtime + OPL3 audio
python scripts/play.py --view --verify-hooks                  # verify mode (lockstep vs ASM)
python scripts/play.py --steps 1000000 --save-snapshot        # headless snapshot for study
python scripts/render_frame.py artifacts/<snapshot> --out frame.png   # VGA PNG dump
```

`--view` runs **unbounded** (until the window closes); `F10` screenshots, `F11`
toggles demo recording, `F12` saves a snapshot. `--speed N` (default 120000
steps/sec) sets the game+music tempo; `--fast-adlib` reaches graphics fastest but
mutes music.

Snapshots and demos (the evidence the source-port work is verified against):

```bash
# In the viewer: F12 saves a snapshot, F11 toggles input-demo recording.
python scripts/play.py --view --record-demo menu_nav        # record from launch
python scripts/play.py --play-demo artifacts/demo_menu_nav_<ts>            # replay (headless, deterministic)
python scripts/play.py --play-demo artifacts/demo_menu_nav_<ts> --view     # watch the replay
```

A demo stores a start snapshot plus VM-visible input keyed to a per-frame clock;
it replays deterministically as long as `chunk_steps`/`timer_irq`/`fast_adlib`
match (recorded in the manifest and reapplied on replay). `--fast-adlib` speeds
cold start but mutes music.

## Style rules

- Write code and comments in English; prefer simple, dependency-free Python.
- Keep replacements readable before making them fast.
- Name lifted helpers after the original address (`sqz_decode_<addr>`) so it is
  obvious when two hooks want the same tail.
- Do not hide weird original behaviour behind clean abstractions until it is
  documented.
- Avoid broad refactors during RE work unless tests/oracle snapshots prove
  behaviour did not change.

## Things not to do

- Do not replace whole systems by guessing formats or intent.
- Do not force suspicious states forward with arbitrary clamps.
- Do not treat corrupted-looking data as a quirk before checking CPU, DOS,
  memory, and hook divergence.
- Do not make the emulator more general than PRE2 requires.
- Do not let `dos_re/` learn anything Prehistorik 2-specific.
- Do not treat performance as proof of correctness.

The VM is not the final architecture. It is the microscope, oracle, and
compatibility harness that lets us recover the real game logic safely.
