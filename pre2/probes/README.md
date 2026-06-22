# pre2/probes/ — temporary observation tools

Short-lived diagnostic/observation code: tracing original control flow, capturing
oracle output, dumping disassembly, locating boundaries. **Temporary scaffolding**
— not in the hot path, not permanent.

Rules:
- A probe only observes; it must not become the place game logic accumulates, and
  it should not look like a canonical replacement/verifier.
- Prune probes once the island they served is recovered and verified (the proof
  lives in `tests/` and the verifier, not in probe scripts).
- Every probe declares its deletion criteria in a one-line `Retire when:` note in
  its module docstring, so probes don't become a junk drawer.

## Active probes

| Probe | Island | Permanent replacement | Retire when |
|---|---|---|---|
| `capture_sb.py` | SoundBlaster audio | `tests/test_sblaster_snapshot.py` + manual play | SB DSP/DMA contract has a headless regression test |
| `capture_sprite_decode.py` / `verify_sprite_decode.py` | sprite decode (42F7/436A) | `tests/test_sprite_decode.py` | already covered — prune next cleanup |
| `capture_blit.py` / `verify_blit.py` | sprite blit (3B69) | `tests/test_blit_renderer.py` | already covered — prune next cleanup |
| `capture_frame_state.py` | frame renderer (Camera/TileMap witness) | `tests/test_frame_bridge.py` | bridge fields stable (kept as the witness regen tool) |
| `verify_frame.py` | tile-row draw (346E) | `tests/test_frame_renderer.py` | a headless 346E lockstep is folded into the test suite |
| `verify_grid.py` | grid redraw (3582) | `tests/test_frame_renderer.py` | a headless 3582 lockstep is folded into the test suite |
| `verify_render_frame.py` | the `render_frame(RendererState)` consolidation seam | `tests/test_render_frame.py` (composition) | a headless render_frame lockstep is folded into the suite (needs a small committed plane fixture) |
| `render_audio_frame.py` | audio diagnostics | — | ad-hoc; delete when audio work closes |

For reference: the SQZ island was recovered with ad-hoc probes (boundary-finding
by watching reads of the compressed buffer; capstone disassembly of dumped bytes;
single-step oracle capture at the routine's `RET`). Those were retired once
`pre2/codecs/sqz.py` was verified. See `docs/pre2/recovery_architecture.md`.
