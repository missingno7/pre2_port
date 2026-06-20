"""Optional provenance links from recovered functions to the original ASM.

An :class:`OracleLink` records *which* original PRE2 boundary a recovered function
replaces, the caller-visible contract it honours, and how far it has been verified.
It is attached out-of-band via the :func:`oracle_link` decorator, which returns the
function unchanged — so recovered code keeps normal signatures and reads like
ordinary source. This is a **testing/documentation aid only**: nothing at runtime
depends on it, and it must never turn recovered logic into an ASM dispatcher.

Tests (and future call-stream diffing) can read ``fn.oracle_link`` to compare the
recovered native call graph against the original ASM call graph without forcing the
runtime to keep calling ASM.
"""
from __future__ import annotations

from dataclasses import dataclass

#: allowed maturity levels, increasing confidence
STATUSES = ("RECOVERED", "VERIFIED", "CANONICAL")


@dataclass(frozen=True)
class OracleLink:
    boundary: str       # original ASM CS:IP replaced, e.g. "1030:346E"
    contract: str       # caller-visible side effects this function reproduces
    status: str = "RECOVERED"

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"OracleLink status {self.status!r} not in {STATUSES}")


def oracle_link(boundary: str, contract: str, status: str = "RECOVERED"):
    """Attach an :class:`OracleLink` to a recovered function (returns it unchanged)."""
    link = OracleLink(boundary, contract, status)

    def _decorate(fn):
        fn.oracle_link = link
        return fn

    return _decorate
