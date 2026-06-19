"""PRE2-specific bootstrap/runtime accelerators.

These are deliberately not source-port gameplay replacements.  They are narrow
helpers that make the original executable usable in the VM while the real game
logic is still being discovered.
"""
from __future__ import annotations

from dos_re.cpu import CPU8086
from dos_re.runtime import Runtime

INNER_SEGMENT = 0x1996


def _near_ret(cpu: CPU8086) -> None:
    cpu.s.ip = cpu.pop() & 0xFFFF


def install_fast_adlib_service(rt: Runtime) -> None:
    """Skip PRE2's hot AdLib tracker service thunk.

    The early Titus/Prehistorik presentation calls a small thunk in the inner
    code segment which tail-jumps into the loaded AdLib/TRK driver at 1C34:0000.
    Interpreting that driver dominates cold-start step time before any gameplay
    is reached.  The hook is intentionally opt-in and PRE2-specific: it mutes the
    tracker updates but leaves the visual/game code running in the original VM.
    """
    for ip in (0x06D5, 0x06DB, 0x06DD):
        key = (INNER_SEGMENT, ip)
        rt.cpu.replacement_hooks[key] = _near_ret
        rt.cpu.hook_names[key] = "pre2_fast_adlib_service_ret"
