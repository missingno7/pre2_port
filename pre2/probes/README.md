# pre2/probes/ — temporary observation tools

Short-lived diagnostic/observation code: tracing original control flow, capturing
oracle output, dumping disassembly, locating boundaries. **Temporary scaffolding**
— not in the hot path, not permanent.

Rules:
- A probe only observes; it must not become the place game logic accumulates, and
  it should not look like a canonical replacement/verifier.
- Prune probes once the island they served is recovered and verified (the proof
  lives in `tests/` and the verifier, not in probe scripts).

For reference: the SQZ island was recovered with ad-hoc probes (boundary-finding
by watching reads of the compressed buffer; capstone disassembly of dumped bytes;
single-step oracle capture at the routine's `RET`). Those were retired once
`pre2/codecs/sqz.py` was verified. See `docs/pre2/recovery_architecture.md`.
