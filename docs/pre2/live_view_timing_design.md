# Live `--view` wait timing — design + as-built

> Status: **IMPLEMENTED** and on by default in live `--view` (`--no-live-cheap-waits` disables), for BOTH
> wait families: the three VGA retrace waits (9900/990D/44CD, retrace-phase waits) AND the PIT-tick delay
> `1C6F` (a `[0x27ee]` timer-counter wait). Sections 1–6 are the original report-first design (written for the
> retrace waits); **§11 is the as-built** for both, with measurements. The deterministic/headless fast-forward
> (shipped, `timing_hook_design.md` §8) remains separate and unchanged — live mode never fast-forwards.

The deterministic and live paths share the same *symptom* (the VGA retrace busy-waits 9900/990D/44CD burn
host CPU) but need **opposite** solutions. Conflating them is the trap this note exists to prevent.

| | deterministic / headless (replay, record, verify, oracle) | live `--view` (realtime) |
|---|---|---|
| Emulated clock | `instruction_count / det_speed` (instructions *are* time) | `perf_counter()` (the wall clock *is* time) |
| What "cheaper waiting" means | execute fewer host instructions for the **same emulated time** | burn less host CPU for the **same wall-clock wait** |
| Correct mechanism | **fast-forward**: advance `instruction_count` across the poll run + re-emit mid-spin IRQs | **sleep/yield**: park game logic, keep servicing IRQs/audio/input, until the same real retrace phase |
| Allowed to change | host work only — emulated timeline byte-identical | host work only — wall-clock & game pacing identical |
| Reproducible? | yes (byte-exact, the contract) | no (already wall-paced; not a contract) |

---

## 1. Why deterministic fast-forward does not apply to live `--view`

In the deterministic paths the emulated clock is `instruction_count / det_speed`, so **advancing
`instruction_count` advances emulated time**. Fast-forwarding the poll run (jumping `instruction_count` past
the spin) is therefore exactly correct: it reaches the same emulated instant with fewer host steps, and the
retrace bit — a pure function of `instruction_count` — flips at the same place. Mid-spin timer IRQs are
re-emitted at the same `instruction_count` so audio/`[0x27ee]` are byte-identical (see `timing_hook_design.md`
§8).

In live `--view` the emulated clock is `perf_counter()` (wall time) — `rt.dos.time_source = perf_counter`
(play.py, the realtime branch). Here `instruction_count` is **not** time; it is just how much work the host
chose to do this wall-frame. If we "fast-forwarded" by jumping `instruction_count` we would change *nothing*
about wall time, and the retrace bit (sampled from `perf_counter`) would not have moved — the wait would not
end. Worse, if a fast-forward were used to *exit* the wait early (skip to the loop's `ret`), the game would
advance into its next frame **earlier than the real retrace allows** → the game runs faster, exactly the
invariant we must not break. So the live mental model is not "skip the wait" — it is "**wait the same amount
of wall time, cheaply.**"

The live goal, stated as preserved vs. changed:

- **Preserve:** same wall-clock pacing · same game pacing · same retrace condition (the loop still exits when,
  and only when, the real retrace phase reaches the value it polls for).
- **Change only:** less CPU burn / less fan / better battery (stop spinning thousands of `in al,dx`).
- **Must NOT become:** a faster game · a shorter wait · "extra instructions after waking to catch up."

---

## 2. How live retrace waits currently pace the game

The realtime branch of `_run_view` (play.py) runs, per displayed frame:

```text
deadline = perf_counter() + present_period          # present_period = 1/present_hz (~14.3 ms @ 70 Hz)
frame_steps = 0
while running and perf_counter() < deadline:
    if live_cpu_budget is None or frame_steps < live_cpu_budget:
        _pump_and_step(now=perf_counter(), n_steps=live_irq_batch=256)   # raise wall-clock PIT/SB IRQs, run 256 instr
        frame_steps += 256
    # else: instruction ceiling spent for this frame — spin the wall clock until the deadline
    if perf_counter() - last_audio >= 0.004:
        sb_audio.pump()                              # keep the audio device fed every ~4 ms
```

The VM steps in 256-instruction batches; each batch first delivers any wall-clock-due timer/SB IRQs, then
runs 256 instructions. When the game reaches a retrace wait it executes the `in/test/je` poll **for real**,
batch after batch, with `_vga_status` reading the retrace bit off `perf_counter()`. The poll keeps returning
CLEAR until the wall clock crosses the retrace phase boundary, at which point the bit reads SET and the loop
`ret`s. So the wait already ends at the correct wall instant — **the game is paced correctly today** — but it
gets there by burning ~`(present_period × instruction_rate)` host instructions per frame spinning. On a
gameplay frame that is ~1.3–1.9 k of real work followed by **thousands of pure poll instructions** (see
`measure_frame_work.py`); that spin is the fan/battery cost.

Two existing knobs bound the loop and matter for any change:
- `present_period` / `deadline` — the frame ends at the wall deadline no matter how few instructions ran (the
  budget is a **ceiling, not a floor**; there is no catch-up).
- `live_cpu_budget = --cpu-hz // present_hz` (default `--cpu-hz 0` → `None` → unlimited): once spent, the loop
  already stops stepping and "waits out the frame" — i.e. the *intended* behavior is exactly "stop computing,
  let wall time pass." The spin is just an expensive way to do that.

---

## 3. Proposed scheduler-friendly wait (park instead of spin)

Replace the busy poll — only inside a *classified* wait, only when safe — with a short-sleep park that keeps
the wall clock advancing and all real-time services alive, then let the VM exit the wait naturally.

```text
when about to step and  cpu.cs:ip ∈ ALL_NODES  (a classified retrace wait)  and  safe_to_park():
    target_phase = the retrace value this loop polls for (SET for 9900; the SET edge for 990D/44CD)
    while perf_counter() < deadline and not input_pending() and running:
        if vga_retrace_phase(perf_counter()) == target_phase:
            break                       # the real retrace condition is now true — stop parking
        service_due_irqs(now=perf_counter())   # PIT/SB timer ISRs MUST still fire on the wall clock
        sb_audio.pump()                        # keep the audio device fed
        pump_sdl_events()                      # so input / window / shutdown are seen promptly
        sleep(min(short_slice, time_until_next_due))   # short_slice ~1–2 ms; yields the core
    # resume normal stepping: the VM's own `in al,dx` now reads SET and the loop `ret`s within a few instr
```

Key properties (the careful part):

- **The slept wall-clock time counts as consumed frame time.** Parking advances `perf_counter()` toward
  `deadline` exactly as spinning would. When we resume, the frame's remaining budget is whatever wall time is
  left — there is **no compensation, no catch-up, no extra instructions** run to "make up" for not spinning.
  If `deadline` is reached while parked, we stop parking and present, with the game still mid-wait, and resume
  parking next frame — identical to how a mid-spin deadline is handled today.
- **We do not exit the loop ourselves.** Unlike the deterministic path we never touch `ip`/`instruction_count`
  to jump to the `ret`. We park until the *real* retrace phase matches, then let the VM execute its own poll,
  read SET, and exit. "Resume only when the original loop would have exited" is enforced by the wall clock,
  not by us. (The game therefore executes *fewer* poll iterations than a full spin would — fine in live mode,
  where `instruction_count` is not a contract; the loops are net-zero-register, side-effect-free polls.)
- **IRQs are not skipped.** A naive `sleep()` over a frame would starve the PIT/SB ISRs — `[0x27ee]` would
  drift and audio would underrun. The park loop must keep delivering wall-clock-due timer/SB IRQs (the same
  `_pump_and_step` pump half), so the only thing removed is the *poll* spin, not the servicing.

---

## 4. Interaction points (must all be preserved)

- **`live_cpu_budget` / `--cpu-hz`:** today, hitting the ceiling already means "stop stepping, wait out the
  frame." Parking is the same intent done cheaply. The park must respect the ceiling identically — it is the
  *spin/idle* portion of the frame that is replaced, never the game-work portion. With `--cpu-hz 0` (default,
  unlimited) there is no ceiling and the whole wait is the retrace spin → the largest win; with a finite
  `--cpu-hz` the win is whatever spin remained after the budget.
- **Audio pump:** must continue on its ~4 ms cadence *throughout* the park (the render/wait can outlast the
  mixer's buffered depth — that is why the current loop pumps every 4 ms, not once per frame). The park's
  sleep slices must be short enough (≤ a few ms) that no pump deadline is missed. SB block IRQ delivery (the
  256-batch cadence that keeps blocks ~on time) must likewise keep firing while parked.
- **Keyboard / input latency:** input is delivered at the canonical per-frame point; a key pressed during a
  park must wake it promptly (≤ one short slice) so latency does not regress versus the spin (which polls SDL
  each outer iteration). `input_pending()` breaks the park early.
- **SDL event handling:** the window must stay responsive (resize, close, screenshot, F11 record toggle). The
  park must pump the SDL event queue each slice, not block the UI thread for a whole frame.
- **Frame deadline:** the park is bounded by `perf_counter() < deadline`; presentation still happens once per
  `present_period`. Parking never delays a present.

---

## 5. Why this is a CPU/battery/noise win, not a smoothness win

Live `--view` is **already** wall-clock paced and presents at `present_hz`; the picture is as smooth as it is
going to get. The retrace wait already ends at the correct instant, so gameplay timing is already correct.
Parking changes **none** of that — it only stops the CPU from executing thousands of useless `in al,dx` polls
while it waits. The visible result is identical frames at identical times; the invisible result is a cooler,
quieter, longer-battery machine (and headroom for the renderer/audio threads). If anyone expects parking to
make live view *look* smoother, that is a sign the mental model has slipped back to "skip the wait" — it must
not. (Smoothness in the *deterministic* paths was a separate, already-shipped concern: `--speed 150000`, the
native-rate default — see `timing_hook_design.md` §10.)

---

## 6. Stop conditions (report before coding further if any hold)

- **It needs an outer-loop rewrite.** If parking cannot be expressed as a localized "step replacement" inside
  the existing realtime loop and instead requires restructuring `_run_view`'s frame/render/event/audio
  scheduling, stop and report the restructure as its own decision (the deterministic pass deliberately avoided
  this; live should too until justified).
- **Audio or input is starved.** If parking causes any SB underrun, missed audio-pump deadline, dropped/late
  input, or UI-event lag versus the current spin, stop — the slice cadence / wake conditions are wrong.
- **Game pacing changes.** If the game advances even slightly earlier or later in wall time (a wait that ends
  before/after the real retrace phase; more instructions executed after waking to "catch up"; the frame
  deadline shifting), stop — the wall-clock contract is broken.
- **Live mode diverges from intended wall-clock behavior.** If parking makes live `--view` behave differently
  from a full-spin run in anything a player can perceive (timing, audio, input, visuals), stop and report.
- **The win is marginal.** If, with the deterministic spin already gone from the tooling paths, the measured
  live CPU saving does not justify the added scheduling complexity and risk, it is fine to *not* implement and
  leave live `--view` as the honest full-spin reference.

---

### Relationship to the shipped work
`pre2/recovered/vga_timing.ALL_NODES` (the classified-wait membership set) is reused here for "are we in a
classified wait." Everything else is live-specific. `_advance_frame_deterministic` and `advance_frame_fast`
remain deterministic-only and unchanged.

---

## 11. As-built (live retrace park)

**Where it lives.** `scripts/play._run_view`, the realtime branch only. Per inner iteration: if
`cpu.cs:ip ∈ ALL_NODES` and `IF` is set (so PIT/SB ISRs can still fire), step a small `_LIVE_POLL_BATCH=32`
re-poll batch instead of the 256-batch, then — bounded by `_time_to_next_retrace_edge(now) − _LIVE_PARK_MARGIN`
(1.5 ms), the audio-pump cadence, and the frame deadline — `time.sleep()` the safe interior of the current
retrace phase. The VM's own `in al,dx` still reads the bit and exits the loop at the same wall-clock instant;
we never touch `ip`/`instruction_count`. `_time_to_next_retrace_edge` mirrors `dos._vga_status`
(`phase = (now·70) mod 1`; SET while `phase ≥ 1−active_fraction`; edges at `1−active_fraction` and the period
wrap). The 1.5 ms margin + busy-poll of the final approach guarantees we never sleep *across* the edge the VM
waits for (which would skip a frame → slow). Toggle: `--live-cheap-waits` / `--no-live-cheap-waits` (default
on). Diagnostics printed at exit: parks, total slept, % of wall yielded, avg/max wait, `unsafe_skipped`.

**Measured (headless, dummy SDL, `pre2/probes/measure_live_park_speed.py` + `--view` smokes):**
- Menu (9900) and carte/map (990D): **~63–76 % of wall-clock time yielded** (the core sleeps instead of
  spinning), `unsafe_skipped=0`, wait avg ≈ one 70 Hz period (~14 ms) — i.e. waits are **not** stretched.
- **Pacing preserved:** retrace-frame rate park-vs-spin drift **+0.8 %** (noise) at equal poll granularity.
  (A naïve comparison showed a spurious +21 % — an artifact of counting wait-exits at different batch sizes,
  not a real speed change.)
- `--video vm` and `--video faithful` both run with parking on (no crash, no `FaithfulVisualGap`).

**Where the live idle actually is** (`pre2/probes/measure_live_waits.py`):

| Scene | dominant live busy-wait | parked? |
|---|---|---|
| Menu | retrace `9900` — 96.5 % | ✅ retrace |
| Carte / map | retrace `990D` — 97.7 % | ✅ retrace |
| **Gameplay** | **PIT-tick spin `1C6F` — 52.4 %** (retrace ~0 %) | ✅ pit |

So the retrace park alone cheapened menu/carte/map/intro but was a **no-op for gameplay**, whose idle is the
`1C6F` PIT-tick spin. That finding drove the PIT extension below.

## 12. As-built — the 1C6F PIT-tick park

`1030:1C6F` (disasm-confirmed) is a *different kind of wait* from the retrace loops — it polls a **memory**
counter, not a port:

```text
1C6F: mov ax,[0x27ee]      ; timer counter, advanced ONLY by the INT 08 timer ISR
1C72: sub ax, cs:[0x1d67]  ; - saved target
1C77: jns 1C7B / 1C79: neg ax   ; ax = abs(counter - target)
1C7B: cmp ax,3 / 1C7E: jb 1C6F  ; loop while abs(delta) < 3  -> wait until ~3 PIT ticks elapse
```

So it is a **wall-clock wait** keyed on the PIT tick, handled exactly as the design (§7) prescribed for live:
while parked in the loop body (`_LIVE_PIT_NODES = {1C6F,1C72,1C77,1C79,1C7B,1C7E}`) with `IF` set, sleep until
the next PIT tick is due (`tick_state["next"]` minus a small `_LIVE_PIT_MARGIN`), bounded by the audio cadence
and frame deadline; the **normal IRQ pump** then delivers IRQ0, the game's INT 08 ISR advances `[0x27ee]`, and
the loop re-checks and exits on its own. We **never write `[0x27ee]`** from the park — only the timer ISR does
(verified: the loop drains and exits during a park where the *only* thing executing is that ISR). Ticks stay
on schedule because `tick_state["next"]` advances by exactly one period per delivery (no `now`-rebasing unless
it fell >0.25 s behind), so there is no drift and no catch-up.

**Measured:**
- Live gameplay yields ~26–45 % of wall (`--view` smoke vs the isolated `measure_live_park_speed.py` probe;
  gameplay also does ~48 % real work, so the wait is the only part that can be yielded). `unsafe_skipped=0`.
- **Pacing exact:** PIT-wait completion rate park-vs-spin drift **+0.0 %** (identical) — the timer-IRQ
  schedule is wall-clock-driven and untouched by parking, so the loop exits at the same instant either way.
- `--video vm` and `--video faithful` gameplay both run with PIT parking on (no crash).

**Combined result.** Menu/carte: retrace park (~60–76 % yielded). Gameplay: PIT park (~26–45 % yielded). Game
timing unchanged in both (drift ≤ +3 %, PIT = 0 %). Deterministic suite unchanged (287). `--no-live-cheap-waits`
disables **both** families. Diagnostics report retrace vs pit parks separately plus total wall yielded. This
completes the live cheap-wait timing branch.

**Not verifiable headless (needs a real display):** visual smoothness, audio underrun, and input latency
under the park are inherent to live `--view`. The design keeps audio pumping every ~4 ms and input at the
existing per-frame cadence (bounded by the frame deadline), but a human check on a real display is the final
gate — same as the user's own earlier `--view` testing.
