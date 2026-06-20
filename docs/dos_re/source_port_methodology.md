# DOS_RE source-port methodology

`dos_re` is the reusable real-mode oracle layer.  It should run original DOS
MZ/COM programs, expose deterministic snapshots/traces, and provide enough
device/DOS behaviour for the target game to reach useful runtime states.

Target-specific knowledge belongs outside `dos_re`.  For this repo, that package
is `pre2`.

This document is the **naming / altitude discipline** that keeps recovery honest.
For the full porting *process* — proof spine, determinism trap, phased roadmap,
and the per-slice lifting loop — see [`dos_re/AI_PORTING_CHARTER.md`](../../dos_re/AI_PORTING_CHARTER.md).

## The one rule

```text
Do not write a source port first and hope it matches.
Exhaust truth from the original first, then let the source port crystallize from that evidence.
```

The original executable is the oracle. A clean native routine is a *hypothesis*
until it is diffed against the original ASM. Never infer behaviour from what
"probably" happens in other DOS games — the only oracle is this executable and
its observed state transitions.

## Evidence ladder

1. Run original code in the VM.
2. Save snapshots at stable boundaries.
3. Identify inputs, outputs, memory writes, registers, flags, and file/device side effects.
4. Add a narrow replacement only after the original behaviour is understood.
5. Keep the original VM path as a regression oracle.

## Status ladder

Every recovered module carries an explicit confidence status. A name may only
climb this ladder on evidence, never on appearance:

```text
GUESS        based on visual similarity or assumption
OBSERVED     seen in memory/file traces, not yet proven
ASM_MATCHED  tied to specific original routines or data formats
VERIFIED     automatically checked against original ASM behaviour
CANONICAL    accepted as the recovered native source of truth
```

A semantic name must be reversible back to evidence:

```text
semantic name -> runtime slot/fields -> verified lifted routine -> original ASM trace/snapshot
```

If that chain does not exist, use a candidate name plus evidence and confidence,
not a definitive label.

## Crystallization pyramid

Recovery starts from very low-level facts and lets higher-level meaning *emerge*.
Early hooks do not need to know whether a slot is a player, projectile, or enemy.
They only need to prove what the original code did at that boundary.

```text
8. Modern / enhanced port layer
7. Semantic game model layer
6. Gameplay archetype layer
5. Game systems layer
4. Runtime object/data model layer
3. Verified lifted routine layer
2. ASM-compatible hook/runtime layer
1. Original binary oracle layer
```

- **Layer 1 — oracle.** The executable, original data files, interpreted ASM,
  snapshots, traces, frame/sound/port captures, file-offset state. Answers "what
  really happened?".
- **Layer 2 — ASM-compatible hooks.** Exact `CS:IP` wrappers that still think in
  registers, flags, offsets, stack shape, handles, continuation IPs.
- **Layer 3 — verified lifted routines.** Source-level reimplementations of
  bounded routines that passed ASM-oracle comparison. A technical name tied to an
  address (`sqz_decode_<addr>`) is correct here; correctness before meaning.
- **Layer 4 — runtime data model.** Stable structures emerge: slots, fields,
  pointer tables, asset records, tile probes, buffers. An object may still be
  just "a record with coordinates, a sprite ref, and a behaviour id".
- **Layer 5 — game systems.** Repeated routines form systems: asset loading,
  file I/O, renderer, input, sound/timer, object update, collision, state.
- **Layer 6 — archetypes.** Only now name player/enemy/projectile/pickup/boss,
  backed by observed ids, field usage, sprites, collision behaviour, call sites.
- **Layer 7 — semantic model.** Levels, scoring, transitions — the game's actual
  rules.
- **Layer 8 — enhancements.** Optional non-vanilla improvements that depend on
  the semantic model rather than replacing oracle work.

Climb by crystallization, not by guessing. A higher-level name is *earned* when
several verified lower-level facts point to the same concept.

### Naming altitude — concrete

```text
ObjectSlot  is a fact from memory.
Enemy       is an interpretation.
```

Bad shape (three layers fused, name unearned):

```python
def run_enemy_ai_from_cpu_memory_and_draw_sprite(cpu): ...
```

Better shape (each function at one altitude):

```python
def run_object_behavior_<addr>(cpu): ...          # layer 2/3, address-named
def decode_object_slot(memory, base) -> Slot: ...  # layer 4, factual
def classify_object(slot, evidence) -> Candidate:  # layer 6, candidate+evidence
```

## Hook lifecycle

Use the same loop for every candidate routine:

```text
observe -> classify -> choose boundary -> build ASM oracle -> implement hook -> verify -> document
```

1. **Observe.** Gather evidence first: coverage/hotspots, executed-address
   traces, snapshots at/before the target, verifier divergence, fail-fast dumps.
   Do not pick a hook just because the address is hot — determine its role.
2. **Classify.** Asset decoder, file/container I/O, renderer primitive,
   coordinate helper, input/menu wait, timer/sound path, object behaviour,
   collision tail, startup table builder, or transient bootstrap/relocation code.
   If unclear, keep it a candidate and gather more evidence.
3. **Choose a boundary.** The smallest coherent unit that can be verified: a leaf
   loop with deterministic I/O, a routine with a clean near/far return, an inner
   loop with a clear continuation IP, or a parent that only composes already
   verified children. Avoid broad parents that hide unverified behaviour.
4. **Build an oracle.** Run the interpreted original ASM from the same entry
   state and record every observable effect the boundary touches.
5. **Implement the hook** as a **thin VM adapter over a pure recovered rule**:
   the adapter reads/writes VM memory and registers and preserves exact return
   mechanics; the rule is side-effect-free, unit-testable game logic that knows
   nothing about CPU/segments/`dos_re`.
6. **Verify** against the ASM oracle (registers + flags + touched/full memory),
   and against the frame oracle for visual paths. A hook that passes only because
   a nested hook hides original behaviour is not proven.
7. **Document** the finding, update the address ledger and status doc, and add a
   test that makes the finding executable.

Never add a hook because it looks right. Every hook needs oracle evidence.

## Bootstrap is extraction, not target gameplay

Packers, DOS launchers, relocation stubs, and self-relocating loaders are not the
source port. They are the oracle's way of preparing itself.

```text
original files -> faithful bootstrap/extraction -> stable initialized snapshot -> clean source-port runtime
```

They may be accelerated in `dos_re` when the algorithm is target-neutral (e.g.
the LZEXE loop), but the clean source port must not grow a permanent dependency
on the unpacker. Run the bootstrap once, snapshot past it, and lift from the
unpacked image.

## Fail-fast over guessed fallback

Fail-fast paths turn unknown behaviour into a precise snapshot + state dump. Do
not replace them with guessed fallbacks merely to keep the game running. When one
triggers: treat the dump as a new oracle candidate, reproduce it, diff original
ASM against the current lift, add the smallest missing branch, and keep the
failure message specific if the branch stays unknown.

## Duplication control

The long-term risk is bloat: a new hook that reimplements an existing tail with
slightly different behaviour. Before implementing one, search for the same
address suffix in function names, the same continuation IP, the same field
offsets, or the same helper in the island module. Prefer shared helpers named
after the original address (`decode_<addr>`, `scan_<addr>`) so it is obvious when
two hooks want the same tail.

## Short rules

```text
1. Fidelity first, readability second, meaning third.
2. No higher abstraction without evidence from the layer below.
3. Refactor must not change behaviour.
4. Fix must not introduce a semantic model.
5. Hooks are minimal boundary adapters, not where logic lives.
6. Factual structure before interpreted name; candidate before definitive.
7. Every semantic name needs an evidence trace.
8. Lower layers must not import higher layers.
9. The original executable stays the oracle until a subsystem is VERIFIED.
```
