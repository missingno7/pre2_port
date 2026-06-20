from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

from .cpu import CPU8086


Hook = Callable[[CPU8086], None]


@dataclass(frozen=True)
class Replacement:
    cs: int
    ip: int
    name: str
    handler: Hook


class HookRegistry:
    """Maps original DOS addresses to Python replacements.

    The intended migration path is:
    1. execute original ASM and collect traces,
    2. understand a small routine,
    3. register a replacement at its CS:IP,
    4. let the rest of the original binary continue running.
    """

    def __init__(self) -> None:
        self.replacements: dict[tuple[int, int], Replacement] = {}

    def replace(self, cs: int, ip: int, name: str):
        key = (cs & 0xFFFF, ip & 0xFFFF)

        def deco(fn: Hook) -> Hook:
            # Fail fast on duplicate registrations.  The map is keyed by CS:IP, so
            # a second @replace at the same address would silently shadow the
            # first; that is exactly how superseded hook implementations used to
            # accrete unnoticed.  One address must have exactly one replacement.
            existing = self.replacements.get(key)
            if existing is not None:
                raise ValueError(
                    f"duplicate replacement at {key[0]:04X}:{key[1]:04X} "
                    f"({existing.name!r} then {name!r})"
                )
            self.replacements[key] = Replacement(key[0], key[1], name, fn)
            return fn
        return deco

    def install(self, cpu: CPU8086) -> None:
        # Allow individual hooks to be disabled without code changes, e.g.
        # DOS_RE_DISABLE_HOOKS=<cs>:<ip>,<cs>:<ip>.  Disabled addresses fall
        # back to the interpreted original ASM, which is useful for A/B
        # performance checks and for bisecting a suspected-incorrect hook.
        disabled = _parse_disabled(os.environ.get("DOS_RE_DISABLE_HOOKS", ""))
        for key, repl in self.replacements.items():
            if key in disabled:
                continue
            cpu.replacement_hooks[key] = repl.handler
            cpu.hook_names[key] = repl.name


def _parse_disabled(text: str) -> set[tuple[int, int]]:
    out: set[tuple[int, int]] = set()
    for token in text.replace(";", ",").split(","):
        token = token.strip()
        if not token:
            continue
        cs, _, ip = token.partition(":")
        out.add((int(cs, 16) & 0xFFFF, int(ip, 16) & 0xFFFF))
    return out


def call_installed_hook_like_near_call(
    cpu: CPU8086,
    key: tuple[int, int],
    default_handler: Hook,
    return_ip: int,
) -> None:
    """Run an installed child hook with original near-CALL stack semantics.

    Source-port parent hooks often compose child routines directly instead of
    letting the VM execute an actual CALL instruction.  This helper preserves the
    CALL/RET stack effect and, when live hook verification is active, routes the
    child through the verifier at its real CS:IP boundary.  Without this, a bad
    lifted child can hide inside a larger verified parent and surface only as a
    later frame/state divergence.
    """
    handler = cpu.replacement_hooks.get(key, default_handler)
    name = cpu.hook_names.get(key, getattr(handler, "__name__", "replacement"))
    call_site = (cpu.s.cs & 0xFFFF, cpu.s.ip & 0xFFFF)
    previous_call_site = getattr(cpu, "hook_call_site", None)
    cpu.hook_call_site = (call_site[0], call_site[1], key[0] & 0xFFFF, key[1] & 0xFFFF, return_ip & 0xFFFF)
    cpu.push(return_ip & 0xFFFF)
    cpu.s.cs = key[0] & 0xFFFF
    cpu.s.ip = key[1] & 0xFFFF
    verifier = getattr(cpu, "hook_verifier", None)
    try:
        if (
            verifier is not None
            and getattr(cpu, "hook_verifier_verify_nested_calls", True)
            and key not in getattr(cpu, "hook_verifier_passthrough", set())
        ):
            verifier(cpu, key, handler, name)
        else:
            handler(cpu)
    finally:
        if previous_call_site is None:
            try:
                delattr(cpu, "hook_call_site")
            except AttributeError:
                pass
        else:
            cpu.hook_call_site = previous_call_site


registry = HookRegistry()


def jump_installed_hook_boundary(
    cpu: CPU8086,
    key: tuple[int, int],
    default_handler: Hook,
) -> None:
    """Run an installed child hook reached by original JMP/fall-through semantics.

    This is the sibling of :func:`call_installed_hook_like_near_call` for
    original control flow that transfers to another ASM routine without pushing
    a return word.  Lifted parent hooks use it when they manually jump or
    fall through into a registered child boundary.  The child still sees its
    real CS:IP, and live hook verification can therefore diff that child
    independently instead of letting it become a shared black box inside the
    parent transaction.
    """
    handler = cpu.replacement_hooks.get(key, default_handler)
    name = cpu.hook_names.get(key, getattr(handler, "__name__", "replacement"))
    jump_site = (cpu.s.cs & 0xFFFF, cpu.s.ip & 0xFFFF)
    previous_jump_site = getattr(cpu, "hook_jump_site", None)
    cpu.hook_jump_site = (jump_site[0], jump_site[1], key[0] & 0xFFFF, key[1] & 0xFFFF)
    cpu.s.cs = key[0] & 0xFFFF
    cpu.s.ip = key[1] & 0xFFFF
    verifier = getattr(cpu, "hook_verifier", None)
    try:
        if (
            verifier is not None
            and getattr(cpu, "hook_verifier_verify_nested_calls", True)
            and key not in getattr(cpu, "hook_verifier_passthrough", set())
        ):
            verifier(cpu, key, handler, name)
        else:
            handler(cpu)
    finally:
        if previous_jump_site is None:
            try:
                delattr(cpu, "hook_jump_site")
            except AttributeError:
                pass
        else:
            cpu.hook_jump_site = previous_jump_site
