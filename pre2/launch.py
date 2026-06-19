"""Launch helpers for the original DOS Prehistorik 2 executable."""
from __future__ import annotations


def build_command_tail(dos_args: bytes | str = b"") -> bytes:
    """Return a PSP command tail for PRE2.EXE.

    At this stage we do not invent game-specific switches.  The packed original
    is launched with the exact tail requested by the caller, defaulting to an
    empty DOS command line.
    """
    if isinstance(dos_args, str):
        return dos_args.encode("ascii", errors="strict")
    return bytes(dos_args)
