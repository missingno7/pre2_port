# Timing-hook design note — recovered VGA retrace / busy-wait primitives

> Status: **PROMOTED — a recovered timing primitive, ON BY DEFAULT in the deterministic hybrid runtime.**
> The classified VGA retrace waits (9900/990D/44CD) are now collapsed in closed form by
> `pre2/bridge/timing_fastforward.advance_frame_fast`, the default deterministic stepper for headless
> replay, in-view demo replay, and verify/oracle runs. It is **byte-equivalent** to the interpreted ASM
> loops (whole memory + all registers + instruction_count identical at every frame boundary; full 1 MB
> snapshot identical end-to-end; identical `--full-verify` divergence set), ~6–15× faster on wait-heavy
> scenes (~2× end-to-end on a mixed demo). It falls back to the pure interpreted loops under
> `--no-replacements` (pure oracle) or `--no-fast-retrace-waits` (keeps the ASM timing path available for
> comparison). **Live `--view` is intentionally untouched** (§7). The canonical timing model is the existing
> `sub_batch` cadence (§8) — deliberately *not* a new exact-tick model, so no second timing model exists and
> nothing is reinterpreted. The contract recovered here is **emulated time**, not pixels: the game must not
> run faster, the emulated timeline must not advance differently, and demo/replay/full-verify determinism
> stays byte-intact. See §8 for the as-built design and §9 for the canonical-model decision.

## 0. Target loops (this pass) and the one we do NOT touch

Disassembled from the GOG build (capstone on the dumped image). All three are CALL'd subroutines that
`push ax`/`push dx`, spin polling a VGA input-status register, `pop dx`/`pop ax`, `ret` — i.e. their **net
architectural effect is zero registers**; they exist only to consume time until a retrace phase.

```
1030:9900  wait_for_retrace_START   (poll 0x3DA bit3 until SET)
  9900 push ax / 9901 push dx / 9902 mov dx,0x3DA
  9905 in al,dx / 9906 test al,8 / 9908 je 9905     ; loop while CLEAR -> exit when SET
  990A pop dx / 990B pop ax / 990C ret
  callers: 97D6

1030:990D  wait_for_retrace_EDGE    (wait while SET -> clear -> then 9900's "until SET")
  990D push ax / 990E push dx / 990F mov dx,0x3DA
  9912 in al,dx / 9913 test al,8 / 9915 jne 9912     ; loop while SET -> falling edge
  9917 in al,dx / 9918 test al,8 / 991A je 9905      ; if CLEAR -> fall into 9905 (wait until SET)
  991C pop dx / 991D pop ax / 991E ret               ; (the 9905 path returns via 990A)
  callers: 9644, 96B2, 9C71

1030:44CD  wait_for_retrace_EDGE    (the present / page-flip vsync; cs:[1] picks monitor)
  44CD push ax / 44CE push dx / 44CF cmp cs:[1],0 / 44D5 je 44E9      ; cs:[1]!=0 (=5) -> COLOR path
  COLOR (0x3DA, ah=8):  44DC in/44DD test ah,al/44DF jne 44DC (while SET) ; 44E1 in/44E2 test/44E4 je 44E1 (while CLEAR) ; 44E6 pop dx/44E7 pop ax/44E8 ret
  MONO  (0x3BA, ah=1):  44EE.. same shape ; 44F8 pop dx/44F9 pop ax/44FA ret
  callers: ~15 (present 307D/3080/3111/32CB.., carte 911A.., 9149/9159/91AA/91C2/91E1/92AB, 4E50/4F0E)
```

`cs:[1]` is 5 on the GOG build → 44CD always takes the **color (0x3DA)** path; the mono path is
disasm-classified but not runtime-exercised, so its primitive is a guarded synthetic case, never assumed.

**NOT in this pass:** `1030:1C6F` — a PIT/timer-tick spin on `[0x27EE]` (advanced by the timer ISR), a
different mechanism (it waits for an ISR-driven counter, not a VGA phase). Left interpreted in pass 1.

All three retrace loops **exit with the retrace status bit SET**.

## 1. The timing model, reconfirmed per mode (current code)

The retrace status bit is computed in `dos_re/dos.py::_vga_status`:

```
phase = (time_source() * display_refresh_hz()) % 1.0          # display_refresh_hz() == 70.0
bit   = retrace_bit if phase >= (1.0 - vga_retrace_active_fraction) else 0x00
# side effect on EVERY read: self._attr_flipflop = False
# if time_source is None: toggles per read via (vga_status_reads += 1) — the headless-no-clock fallback
```

So the retrace phase is a pure function of `time_source()`, and `time_source` differs by mode:

| Mode | `dos.time_source` | What drives phase | Frame budget | IRQ delivery cadence |
|---|---|---|---|---|
| **live `--view`** (`realtime`) | `perf_counter` (WALL clock) | real wall time | `while perf_counter() < now+present_period` (no fixed budget) stepping `live_irq_batch`=256 | per 256-step batch, IRQs raised on wall clock |
| **demo replay / record** (`_run_view`, not realtime) | `det_now = base + instruction_count/det_speed` | **instruction_count** | `chunk_steps` **cpu.step() calls** (`_advance_demo_frame`) | per `sub_batch`=2000 step() batch (`_pump_and_step`, `now=clock()` sampled once per batch) |
| **headless demo** (`_run_replay_headless`) | `det_now = instruction_count/det_speed` | **instruction_count** | `chunk_steps` cpu.step() calls | per `sub_batch`=2000 step() batch |
| **`--verify-hooks`** (contract oracle) | the det clock of whatever run hosts it | instruction_count | step()-count (host run) | per sub_batch; the ASM spin RUNS as the oracle (hooks pass through at the wait loops — they are not in the verify set) |
| **`--full-verify`** (differential) | host run's det clock | instruction_count | per sub_batch; per hook it CLONES the asm cpu at `src.cpu.instruction_count` and `_run_to`s the ASM oracle to the hook's stop, comparing the contract | n/a (transactional) |

`det_speed = chunk_steps * present_hz` (≈ 450000). So in the deterministic modes one **emulated second** =
`det_speed` instruction_count, the retrace bit cycles at 70 Hz over instruction_count, and a busy-wait spin
advances the clock purely by **incrementing instruction_count** as it iterates.

Key consequences:
- **Deterministic modes:** the spin's only job is to advance `instruction_count` until the phase crosses;
  the host cost is the interpreted polls. The clock and the spin are the SAME thing.
- **Live `--view`:** the spin polls `perf_counter`; it burns host CPU until real wall time crosses the phase
  — the spin IS the wall-clock pacing within a frame.

## 2. Why a naive skip is wrong (the load-bearing facts)

Two independent mechanisms make a naive "detect loop → skip → resume" break byte-equivalence:

1. **The deterministic frame budget counts `cpu.step()` invocations, not `instruction_count`.**
   `_advance_demo_frame` runs exactly `chunk_steps` `cpu.step()` calls per frame (`_pump_and_step`:
   `for _ in range(n_steps): cpu.step()`). A replacement hook is **one** `cpu.step()` call. If that hook
   collapses a ~6000-iteration spin, the frame still has its full `chunk_steps` budget left → it executes
   ~6000 MORE game instructions that frame → the program advances further per frame than pure ASM →
   **the demo/replay trajectory diverges and the game effectively runs faster.** (For pure ASM and ordinary
   hooks, 1 `cpu.step()` ≈ +1 `instruction_count`, so the step budget and an instruction_count budget agree
   — they only diverge for a fast-forward hook.)

2. **Timer IRQs are delivered at `sub_batch` (2000-step) boundaries, mid-spin.**
   During a real spin of N steps, `_pump_and_step` is re-entered every 2000 steps; each re-entry samples
   `now=clock()` and raises/services the timer IRQ for every PIT tick up to `now`. So a 6000-step spin gets
   ~3 IRQ-delivery points **inside** it, and the timer ISR runs there (advancing `[0x27EE]`, the 1C6F gate,
   servicing SB/DMA). A single fast-forward hook executes in ONE step() with no interior `_pump_and_step`
   re-entry → those mid-spin IRQs are **never delivered at the right emulated time** → timer/audio/ISR-driven
   memory diverges (and the 1C6F timer-spin, which reads `[0x27EE]`, then behaves differently).

Therefore a correct fast-forward is **not** "advance instruction_count to the exit." It is "advance
instruction_count to the exit **AND** deliver exactly the IRQ events that would have fired during the skipped
interval, at the same emulated-time points, in the same order **AND** consume the same amount of frame
budget." That requires changing the budget to an instruction_count basis and re-emitting mid-spin IRQs —
see §4/§5.

## 3. Loop classification (taxonomy)

```
1030:9900  -> wait_for_retrace_start   (poll until bit SET)
1030:990D  -> wait_for_retrace_edge    (wait SET->clear, then until SET; shares 9905 tail)
1030:44CD  -> wait_for_retrace_edge    (color 0x3DA / mono 0x3BA; the present/page-flip workhorse)
1030:1C6F  -> PIT_tick_wait            (NOT this pass)
```

Only `{9900, 990D, 44CD}` are candidates in pass 1. No global hooking of port-0x3DA reads — only these
concrete, disassembled, classified CALL entry points; any other 0x3DA read stays interpreted (it may carry
semantic side effects we have not proven inert).

## 4. The per-loop timing contract (what a replacement MUST preserve)

For each hooked loop, the recovered primitive must reproduce, exactly:

- **entry IP** (the CALL target) and **exit IP** (the post-`ret` address popped from the stack).
- **the condition waited for** (retrace bit SET at exit) and the **final VGA retrace phase** at the exit
  instruction_count (so the next `_vga_status` read by game code returns the same value).
- **architectural registers:** net-zero (ax/dx pushed then popped) → unchanged from entry. `bp/si/...`
  untouched.
- **flags:** the loop exits right after a `test` that found the bit SET (ZF=0). Preserve the final flags
  (the callers are CALLs-for-timing and re-load/ignore, but the contract is the exact `test` result).
- **stack / return:** `push ax;push dx; … ;pop dx;pop ax; ret` nets to "pop the near return address"
  (SP += 2 over entry; the two popped scratch words remain as dead bytes below SP, exactly as the ASM
  leaves them).
- **port-read side effects:** every `in al,dx` resets `_attr_flipflop = False`; the count of reads is
  irrelevant to that (it ends False). Under the deterministic clock `vga_status_reads` is NOT touched.
- **`instruction_count` advancement:** advance by EXACTLY the number of instructions the spin would have
  executed (closed-form, validated against the interpreter — Stage C).
- **IRQ/timer/audio cadence:** re-emit every timer IRQ (and SB service) that would have been delivered at a
  `sub_batch` boundary inside the spin, at the same emulated-time points and order — §5.
- **frame-budget accounting:** consume the same amount of per-frame budget the spin would have (requires the
  instruction_count-delta budget — §2.1).
- **replay determinism:** the resulting instruction stream / state must be byte-identical to pure ASM at
  every checkpoint and at demo end.

If ANY of these is unknown or unproven for a loop, that loop is NOT live-hooked (fail loud, leave ASM).

## 5. Mid-spin IRQ handling (the hard part — design)

A fast-forward over a spin of `N` instructions starting at `instruction_count = ic0`:

- **Skipped interval:** `[ic0, ic0+N)`. `N` is the exact spin length (Stage C closed-form).
- **Where IRQ boundaries fall:** in the deterministic modes IRQs are serviced at the start of each
  `sub_batch`. The sub_batch boundaries are a function of the `_advance_demo_frame` loop counter, not of
  `instruction_count` directly — which is exactly why the budget must move to an instruction_count basis
  first. Once the budget is instruction_count-delta, the IRQ-delivery points become well-defined emulated-
  time points: a PIT tick is due whenever `clock()` crosses `tick_state["next"]`, i.e. at
  `instruction_count` values `ic` where `base + ic/det_speed >= tick_state["next"]`.
- **How to service them in order:** the primitive must, for each PIT tick whose due-time lies in
  `[ic0, ic0+N)`, set `instruction_count` to that tick's `instruction_count`, raise/deliver IRQ0 (and run
  the ISR via the same `deliver_interrupt` path), service the SB, then continue the fast-forward to the next
  tick or to `ic0+N`. This reproduces the exact ISR cadence the interpreted spin produced.
- **Determinism:** because the tick due-times are a pure function of `instruction_count` and `det_speed`,
  the re-emitted cadence is identical to the interpreted run's, for the same `chunk_steps`/`present_hz`.

This is a **timing-system change** (budget basis + IRQ re-emission), not a local hook. Per the stop rules it
is designed here but only implemented behind an experimental flag after Stage C proves the closed-form exit,
and only enabled by default after Stage E proves full equivalence.

## 5b. Stage B measurements (pre2/probes/measure_retrace_waits.py, non-invasive)

| Scene (snapshot) | loop | step() share in waits | entries | spin_ic avg/max | poll-iters avg | ~ISR-instr/spin | mid-spin IRQs |
|---|---|---|---|---|---|---|---|
| CARTE present (210538) | 990D edge | **97.7%** | 202 (~1/frame) | 6271 / 6298 | 2078 | ~37 | 207 (~1/spin) |
| MENU (075918) | 9900 start | **93.6%** | 481 (~4/frame) | 1503 / 6012 | 497 | ~12 | 128 (~0.27/spin) |

PIT tick interval ≈ 6179 instructions (det_speed/pit_hz). So a full-period spin (~6271 ic) spans ≈ one PIT
tick → ~1 timer IRQ is delivered **inside** it; shorter spins span < 1 tick → often 0. The spin's
instruction_count delta is ~`poll_iters*3` poll instructions **plus** the mid-spin ISR instructions.
Implication for the fast-forward: it must **interleave fast-forwarded poll segments with REAL ISR delivery**
at the sub_batch/PIT boundaries — the polls (the ~6234-instruction bulk) are computed; the ISR (~37
instructions) runs for real. The closed-form (Stage C) therefore predicts a **poll-only segment** exit; the
hook (Stage D) calls it repeatedly between ISR deliveries.

## 6. Staged plan & stop conditions

- **Stage A (this note):** design + reconfirmation. ✅ no behavior change.
- **Stage B:** measurement-only probes (entry counts, spin lengths in iterations + step() consumed, step()
  share, IRQs during spins, per-scene hotness). No behavior change.
- **Stage C:** a pure closed-form simulator predicting each loop's exit `instruction_count` + final bit +
  iteration count + port-read side effects under the deterministic clock, with a probe asserting the
  prediction matches the interpreted ASM exactly. No live hook.
- **Stage D:** exact fast-forward hook behind an explicit experimental flag (off by default), preserving the
  full §4 contract and §5 IRQ re-emission, resuming at the exit IP.
- **Stage E:** full verification — pure ASM vs hooked primitive: same memory at checkpoints, same frame
  outputs, same demo trajectory, same audio/timer-visible state, no faster pacing, no full-verify
  divergence.
- **Stage F:** only then default-enable for deterministic/headless/verify modes. Live `--view` is separate
  (§7).

**Stop and report (do not code further) if:** instruction_count equivalence is unclear; the step budget must
be redesigned (it must — that is itself a reported decision point); mid-spin IRQ handling is needed but not
isolated; live wall-clock pacing needs a rewrite; full-verify diverges; or the work starts spreading into
renderer/audio code. No silent fallback, no approximate timing, no shims.

## 7. Live `--view` is separate — design required before any code

In live mode the spin is the wall-clock pacing *within* a frame, and the outer loop already busy-waits to the
present deadline. Fast-forwarding the retrace polls there would make the game run **faster in game time**
(the opposite of the invariant) — so the deterministic fast-forward is, correctly, NOT applied to live
`--view` (the realtime branch never routes through `_advance_frame_deterministic`). The live goal is
different: *don't burn a core while waiting*, without changing pacing. That needs an **outer-loop** change,
not a wait-loop replacement:

- Detect that the VM is parked in a classified retrace wait (`cpu.s.ip in ALL_NODES`) with no input/audio
  work pending, and instead of spinning the interpreter, `sleep`/yield until either the next present
  deadline or the wall-clock instant the retrace phase next flips — then resume the VM exactly where it was.
- The VM's emulated clock is wall-clock (`perf_counter`) in live mode, so "where the retrace bit flips" is a
  real future timestamp; the game-time advance must be identical to having spun (same number of emulated
  instructions would have run), i.e. the sleep must be accounted into the per-frame instruction budget so we
  do not under- or over-run game time.
- Audio must keep pumping during the sleep (the mixer thread is fed on a few-ms cadence today), and input
  must stay responsive (wake early on a key event).

This was the separately-scoped live pass, now **implemented** for the three classified retrace waits (on by
default, `--no-live-cheap-waits` to disable): while parked in 9900/990D/44CD the live loop sleeps through the
safe interior of the retrace phase and busy-polls only the last ~1.5 ms before an edge, so the VM's own poll
exits at the same wall-clock instant — same pacing (+0.8 % drift), ~63–76 % of wall yielded on menu/carte.
Full design + as-built + the key scope finding (live *gameplay* idles in the `1C6F` PIT-tick spin, which is
outside the classified-retrace scope and is **not** parked — reported for a go/no-go) are in
**`docs/pre2/live_view_timing_design.md`** (§11). The deterministic/headless/verify speedup (the 86%-hot path
for tests and tooling) was the first and separable target, shipped (§8).

## 8. As-built — the promoted recovered timing primitive (and why it differs from §5)

The shipped design is **simpler and strictly safer** than the §5 "instruction_count-delta budget + deliver
IRQ0 at the exact tick due-ic" sketch, and was chosen because §5 would have produced a *different* (if more
physically-accurate) timeline that reinterprets the existing deterministic execution for no behavioural gain.
Two deliberate departures:

1. **Keep the existing IRQ cadence; do not change the budget basis.** `pre2/bridge/timing_fastforward.py`
   `advance_frame_fast` mirrors `play._advance_demo_frame` exactly: the same `sub_batch` (2000-instruction)
   boundaries, the same per-boundary pump (`_pump`, a verbatim copy of `_pump_and_step`'s tick/SB/PIC half).
   IRQs are still delivered only at those boundaries — *not* at exact PIT-tick due-ic. The fast-forward only
   collapses poll iterations **between** boundaries, where the interpreted model delivers no IRQ anyway. So
   the result is byte-identical to the current stepper — **no new timeline, no demo re-recording** (resolves
   constraint #8). The "mid-spin IRQ" of §5/§5b is handled trivially: a spin longer than 2000 instructions
   simply spans several sub_batches, and the pump runs (delivering the timer ISR for real) at each boundary,
   exactly as today.

2. **Interpret boundaries for real; bulk-skip only provably-identical poll runs.** Rather than reconstruct
   the loop's mid-spin register/flag state in closed form (fragile — the ISR pushes FLAGS/CS/IP, and `in
   al,dx` / `test` / `cmp` flag bits would all have to be re-derived), `_fast_forward_wait` runs every
   boundary instruction with the real `cpu.step()` (entry setup, each loop's first iteration, loop-to-loop
   transitions, the `pop/pop/ret` exit). It collapses *only* a run of identical poll iterations: on arriving
   at a loop-top via its back-edge (`_POLL_BACKEDGE`), where flags are known to be `test(continue_bit)`, it
   advances `instruction_count` by `3·k` over the `k` consecutive same-condition iterations that fit before
   the boundary. Each skipped iteration returns to the same loop-top with identical ip/registers/flags — only
   the deterministic clock (hence the next sampled retrace bit) moves — so the skip is exact by construction.
   `pre2/recovered/vga_timing.py` keeps only the loop CFG tables (`LOOP_GRAPHS` → `ALL_NODES`, the
   membership test) and the Stage-C entry simulators; the generalized `walk_loop` was removed as unused.

**Retrace sampling.** `make_sample(det_speed, base, active_fraction)` reproduces `dos._vga_status`'s SET test
(`(base + ic/det_speed)·70 mod 1 ≥ 1 − active_fraction`). `active_fraction` is read from
`dos.vga_retrace_active_fraction` at the call site so it always matches the VM; `base=0` on the headless
deterministic clock.

**Verification gates.**
- `tests/test_timing_fastforward.py` (committed, snapshot-free): a mock CPU interpreting `ALL_NODES` proves
  the bulk-skip leaves the identical `(instruction_count, ip)` as naive single-stepping across a sweep of
  clock phases and budgets, and that a full-frame budget always exits the loop. Guards the skip arithmetic.
- `pre2/probes/verify_fast_retrace.py` (snapshot-based, manual — snapshots are gitignored): drives two
  runtimes from the same snapshot, one with `play._advance_demo_frame`, one with `advance_frame_fast`, and
  asserts whole-memory + all-registers + instruction_count identical at every frame boundary for 80 frames
  across carte (990D) / menu (9900) / gameplay (44CD). PASS, speedups 14.6× / 6.0× / 1.0×.
- End-to-end `--play-demo` (default fast) vs `--no-fast-retrace-waits` → identical `memory_1mb.bin`,
  `state.json`, `trace_tail.txt` at 800k and 4M instructions; 24.0 s → 13.3 s wall on the 4M replay.
- `--full-verify` (whole-machine ASM-oracle diff after every recovered routine) with fast on vs off →
  **identical divergence set** (the only divergences are the pre-existing known mixer "channel-state"
  recovered-vs-oracle bug at `1A0F:0B60/0B62/10B9/10BA` — unrelated to retrace timing; see
  [[pre2-renderer-effect-bugs]]). The fast path introduces no new divergence; audio/tracker/timer-visible
  state matches. (Check *counts* differ only because full-verify's oracle re-execution inflates
  `instruction_count`, so a fixed `--steps` budget covers a different program distance — not a divergence.)

## 9. Canonical timing model & integration (the promotion decision)

**Decision: the canonical deterministic timing model is the existing `sub_batch` cadence; we did NOT switch
to an exact-tick instruction_count-delta model.** Rationale:
- It is already the *single* model every deterministic path uses; `advance_frame_fast` is byte-equivalent to
  it, so promoting introduces **no second model** and changes **no** captured checkpoint/oracle baseline.
- `1 cpu.step() == 1 instruction_count` already, so the "budget basis" is instruction-count; `sub_batch=2000`
  is only the IRQ-pump granularity. An exact-tick model would change the timeline for *all* deterministic
  runs and invalidate every golden for zero behavioural gain (the retrace waits self-synchronise to vsync
  regardless of where in a 2000-instruction window the timer IRQ lands).
- Consequence for demos: existing recordings **replay byte-identically** under the promoted model, so no
  re-recording is forced and no compatibility shim exists. (If we ever deliberately move to exact-tick later,
  that is the moment to delete/re-record demos — not now.)

**Integration.** `_advance_frame_deterministic(rt, args, …)` in `scripts/play.py` is the single decision
point shared by every deterministic stepping path (headless replay, in-view demo replay, verify/oracle). It
uses `advance_frame_fast` (the recovered primitive) when the hybrid runtime is active — the **default** — and
falls back to the interpreted `_advance_demo_frame` (pure-ASM retrace loops) under `--no-replacements` (pure
oracle) or `--no-fast-retrace-waits` (keeps the original timing path available for comparison, like every
other recovered island keeps its ASM oracle). There is **no experimental flag**; the feature is on by
default and proven. Live `--view` realtime is the one path that never routes through this dispatcher (§7).

**Scope held.** All *deterministic* stepping paths (headless replay `_run_replay_headless`, the in-view demo
replay/record branch, verify/oracle) — routed through `_advance_frame_deterministic`. Live `--view` realtime
is untouched (§7 still applies — it would need the outer-loop pacing rewrite). No renderer / audio-mixer /
gameplay logic changed; the only side effects are the very instructions the interpreted loop runs. `44CD` is
encoded for the COLOR (`0x3DA`) path only; the mono `0x3BA` path is deliberately not fast-forwarded.

## 10. The demo-clock `--speed` default (PRE2's native CPU rate) — `--speed 150000`

Measured with `pre2/probes/measure_frame_work.py` (splits each game-frame at the 44CD present-wait into
*work* = instructions between presents and *spin* = the retrace busy-wait):

| Scene | per-frame WORK (instr) | present SPIN @ speed 450k | "fills a 70 Hz frame" speed = work·70 |
|---|---|---|---|
| Gameplay (185902) | mean 1320, p90 1889, max 20318 | mean 5454, p90 6428 | mean 92k, **p90 ≈ 132k** |

PRE2 is **frame-locked to the 70 Hz VGA retrace** (one 44CD present-wait per frame), so its *game* speed is a
constant 70 fps in emulated time regardless of `--speed`; `--speed` only sets how many instructions the demo
clock packs into each retrace period, i.e. how much *idle spin* pads the ~1.3–1.9 k of real work. At the old
default `--speed 450000` a gameplay frame is **~99 % spin** (6428-instr chunk, ~1.3 k work). Two independent
ceilings both point at ~150 k:
- **Game-native rate.** `p90_work · 70 ≈ 132 k` is the rate at which the real per-frame work nearly fills one
  70 Hz frame with minimal spin — i.e. roughly the effective speed of PRE2's ~1993 target CPU (a fast
  286/386). `--speed 150000` sits just above it (headroom for busier frames; truly heavy frames, e.g. a level
  load spiking to ~20 k instr, *should* slow down — that is faithful to the original on period-correct
  hardware).
- **Host interpreter throughput.** The Python VM sustains ~270 k emulated-instr/s for this workload, so the
  demo loop keeps up with real time (smooth ~70 Hz present) only while `--speed ≲ 270 k`. At 450 k it runs at
  ~0.6× real time and trips the deliberate 4 Hz render fallback (`render_gap = 0.25`, [play.py] §1409) — the
  "4 fps, huge frameskip" symptom. At 150 k it runs ~1.8× real time → smooth, for both record and replay
  (both use the deterministic clock; recording sets `realtime=False`).

So `--speed 150000` is the new default: near PRE2's native rate, comfortably under the host ceiling, minimal
wasted spin. (Note: this is *orthogonal* to fast-retrace — fast-forwarding makes the spin cheap on the host,
but the spin still consumes *emulated* time, so a too-high `--speed` still maps fewer presented frames to each
host-second. Lowering `--speed` to the native rate removes the spin from emulated time itself.) Existing demos
carry their own recorded speed in the manifest and are unaffected; new recordings use 150 k.
