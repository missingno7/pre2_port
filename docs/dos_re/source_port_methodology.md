# DOS_RE source-port methodology

`dos_re` is the reusable real-mode oracle layer.  It should run original DOS MZ/COM programs, expose deterministic snapshots/traces, and provide enough device/DOS behaviour for the target game to reach useful runtime states.

Target-specific knowledge belongs outside `dos_re`.  For this fork, that package is `pre2`.

## Evidence ladder

1. Run original code in the VM.
2. Save snapshots at stable boundaries.
3. Identify inputs, outputs, memory writes, registers, flags, and file/device side effects.
4. Add a narrow replacement only after the original behaviour is understood.
5. Keep the original VM path as a regression oracle.

## Bootstrap policy

Packers, DOS launchers, and relocation stubs are not the game source port.  They may be accelerated in `dos_re` when the algorithm is target-neutral, but gameplay/source logic should crystallize in the target package.
