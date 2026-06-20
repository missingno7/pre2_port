# Prehistorik 2 port architecture

The long-term goal is a behaviour-exact source port grown from verified 8086 ASM
hooks. Code spans a spectrum from "still essentially the original ASM, proven
against the VM" to "clean, backend-agnostic native source".

The guiding direction: **the VM should become an oracle/test harness, not the
engine.** Higher (ASM-bound) layers may depend on lower (cleaner) layers; lower
layers must never depend back up on the VM/CPU/segment world.

> **Current state (recovery phase).** Bootstrap is done: the `dos_re` VM runs
> **PRE2 gameplay**, and recovered native code is now part of the normal runtime.
> The first recovered island — **SQZ asset decompression** — exists as clean
> VM-independent logic (`pre2/codecs/sqz.py`, the *pure* layer) behind a thin
> *replacement adapter* (`pre2/replacements.py`, the *hook_boundary* layer), with
> contract-level verification. The hybrid runtime is the default. The higher
> layers below are now partly real, partly target shape; new code should land in
> the layer it belongs to. The companion north-star doc is
> [`docs/pre2/recovery_architecture.md`](docs/pre2/recovery_architecture.md).

## Execution modes

Three explicit, mode-controlled paths — the original ASM only runs in oracle and
verify modes, never as a silent fallback:

| Mode | What runs | Use |
|------|-----------|-----|
| **oracle / original** | pure original ASM (`native_replacements=False`) | reference, observation, capturing oracles |
| **hybrid (default)** | recovered native replacements, no per-step verification | normal play, demo/snapshot recording |
| **verify** | ASM oracle + recovered logic, diffed at contract boundaries (`--verify-hooks`) | offline proof against recorded demos/snapshots |

**No silent fallbacks.** If the hybrid runtime reaches unrecovered behaviour it
**fails loud** with a precise gap report (`Pre2HybridGap`), turning the gap into
the next task instead of hiding it.

## Packages

```text
dos_re/      reusable, game-independent real-mode VM + verification engines
pre2/        Prehistorik 2-specific recovery layer (see structure below)
nuked_opl3/  vendored optional OPL/AdLib backend (independent of dos_re and pre2)
```

Hard boundary: `dos_re` must not import `pre2` or know any Prehistorik 2 address,
asset name, or format. See
[`docs/architecture/package_boundary.md`](docs/architecture/package_boundary.md).

### `pre2/` structure (current + intended)

So recovered islands land consistently as they are added. Each recovered island
is *clean VM-independent logic* + a *thin adapter* + a *verifier*; the adapters
and verifiers are scaffolding, the recovered logic and (later) dataclass state
mirrors are the real source port.

```text
pre2/
  runtime.py        launch/snapshot wiring; installs the hybrid replacements   [exists]
  replacements.py   active replacement adapters (thin hooks) + verify wiring    [exists]
  bootstrap_hooks.py bootstrap-only helpers (LZEXE/AdLib), never gameplay        [exists]
  codecs/           recovered VM-independent asset codecs (sqz.py: LZSS/LZW/...) [exists]
  recovered/        recovered VM-independent gameplay logic (player/object/...)  [for next islands]
  bridge/           memory views: VM memory <-> recovered structs/dataclasses    [for first stateful island]
  checkpoints/      verification contact points (verifiers/checkpoints)          [grows out of replacements.py]
  probes/           temporary observation/diagnostic tools                       [as needed]
```

`codecs/` and `recovered/` are the **pure** layer (no `cpu`/`mem`/`dos_re`).
`bridge/` is the one place VM memory meets recovered dataclasses. `replacements.py`
and `checkpoints/` are the *hook_boundary* — thin, no game logic.

## Target layers (high = closest to ASM, low = closest to pure source)

| Layer | Role | May depend on |
|-------|------|---------------|
| **vm / orchestration** | `dos_re`: interpreter, hook verifier, frame verifier, snapshots, coverage | anything |
| **hook_boundary** | thin `@registry.replace` wrappers: register an address, set up CPU/stack/return mechanics, delegate. **No gameplay/render/audio logic.** | lifted, bridge, pure, vm |
| **lifted** | VM-aware Python reproducing an original routine on the original memory layout, byte/flag-exact | bridge, pure, vm |
| **backend** | backend-specific rendering / sound / asset codecs / file I/O | pure, bridge, vm |
| **bridge** | typed views/adapters projecting VM/DOS memory ⇄ portable records — the one place CPU/mem meets domain | pure, vm |
| **pure** | portable, VM-free game logic and data records: no `cpu`/`mem`/`dos_re` | pure only |

Dependency direction is upward only:

```text
original oracle -> ASM/VM -> hook boundary -> lifted routines
  -> runtime model -> systems -> semantic entities -> enhanced port
```

### Hard dependency rules

1. The **pure** layer must not import the VM (`dos_re`), hooks, any backend, or
   the bridge. It must stay reachable without the emulator — it is the future
   native source core.
2. **backend** must not import gameplay/systems logic; backends sit behind a
   boundary and never reach up.
3. A view/adapter may know layout (segment:offset, strides, table bases) but
   holds **no gameplay decisions** — those live in the pure layer and are
   replayed by the lifted hook.

### Where new code goes

- Reproducing an original routine that still touches CPU/memory → **lifted**,
  with a thin wrapper in the hook boundary.
- A portable rule with no VM concepts → **pure**.
- Backend-specific drawing/sound/asset work → **backend**.
- The memory projection between them → **bridge** (typed views).

## Snapshot model: checkpoints, not hook boundaries

A registered hook address is **not** automatically a permanent source-port
boundary. Treat the two runtimes differently:

- **The VM (original ASM) stays instruction-level** snapshotable/steppable — it
  is the oracle, and every historical `CS:IP` is observable there.
- **The source-port runtime is checkpoint-level** snapshotable. It resumes only
  from stable *logical* boundaries — **frame, object-update, render, input** (and
  hardware/environment waits). Between two checkpoints, lifted native code may run
  as one atomic deterministic chain; it need not preserve every old `CS:IP`
  bounce. A snapshot requested mid-chain is the previous checkpoint + replay.

So classify each hook by **role**, not address:

| Role | Meaning | Direction |
|------|---------|-----------|
| **checkpoint** | a real logical resume boundary (frame/object-update/render/input) | keep, make explicit |
| **env_wait** | hardware/environment wait (PIT/IRQ0 timer, CRTC retrace, INT 09h) the interpreter can't satisfy natively | keep hooked, even on the oracle reference |
| **debug_probe** | exists only to observe/verify | keep out of the hot path |
| **glue** | accidental ASM-boundary plumbing (tails, helpers, per-row scan steps) | collapse into native chains between checkpoints |

Correctness during any such collapse is protected by the semantic frame/state
verifier against the VM — not by preserving historical hook boundaries.

## The method

The full porting process — the per-slice lifting loop, the proof spine, the
determinism/boundary-clock trap, and the phased roadmap from "lift rules" to
"flip the engine, keep the VM as oracle" — lives in
[`dos_re/AI_PORTING_CHARTER.md`](dos_re/AI_PORTING_CHARTER.md). The
naming/altitude discipline lives in
[`docs/dos_re/source_port_methodology.md`](docs/dos_re/source_port_methodology.md).
