from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .memory import Memory, linear


REG16 = ["ax", "cx", "dx", "bx", "sp", "bp", "si", "di"]
REG8 = ["al", "cl", "dl", "bl", "ah", "ch", "dh", "bh"]
SREG = ["es", "cs", "ss", "ds"]
# Segment-override prefix byte -> register name.  Module-level so the hot
# prefix-decode loop in step() does not rebuild the dict on every instruction.
_SEG_OVERRIDE = {0x26: "es", 0x2E: "cs", 0x36: "ss", 0x3E: "ds"}
JCC_NAMES = ["jo", "jno", "jb", "jnb", "jz", "jnz", "jbe", "ja", "js", "jns", "jp", "jnp", "jl", "jge", "jle", "jg"]

CF = 0x0001
PF = 0x0004
AF = 0x0010
ZF = 0x0040
SF = 0x0080
TF = 0x0100
IF = 0x0200
DF = 0x0400
OF = 0x0800

# Even-parity lookup for the low byte (PF set when the number of 1 bits is even).
_PARITY = [bin(i).count("1") % 2 == 0 for i in range(256)]


class UnsupportedInstruction(NotImplementedError):
    pass


class HaltExecution(Exception):
    pass


@dataclass
class CPUState:
    ax: int = 0
    bx: int = 0
    cx: int = 0
    dx: int = 0
    sp: int = 0
    bp: int = 0
    si: int = 0
    di: int = 0
    cs: int = 0
    ds: int = 0
    es: int = 0
    ss: int = 0
    ip: int = 0
    flags: int = 0x0202

    def snapshot(self) -> str:
        return (
            f"AX={self.ax:04X} BX={self.bx:04X} CX={self.cx:04X} DX={self.dx:04X} "
            f"SI={self.si:04X} DI={self.di:04X} BP={self.bp:04X} SP={self.sp:04X} "
            f"CS:IP={self.cs:04X}:{self.ip:04X} DS={self.ds:04X} ES={self.es:04X} SS={self.ss:04X} "
            f"FLAGS={self.flags:04X}"
        )


@dataclass
class EffectiveAddress:
    segment: str
    offset: int
    text: str


class _RegOperand:
    """Register r/m operand.  Module-level so it is not rebuilt every instruction."""
    __slots__ = ("cpu", "rm", "bits", "text")

    def __init__(self, cpu: "CPU8086", rm: int, bits: int) -> None:
        self.cpu = cpu
        self.rm = rm
        self.bits = bits
        self.text = REG8[rm] if bits == 8 else REG16[rm]

    def read(self) -> int:
        return self.cpu.get_reg8(self.rm) if self.bits == 8 else self.cpu.get_reg16(self.rm)

    def write(self, value: int) -> None:
        if self.bits == 8:
            self.cpu.set_reg8(self.rm, value)
        else:
            self.cpu.set_reg16(self.rm, value)


class _MemOperand:
    """Memory r/m operand bound to a decoded effective address."""
    __slots__ = ("cpu", "segment_name", "offset", "bits", "text")

    def __init__(self, cpu: "CPU8086", ea: EffectiveAddress, bits: int) -> None:
        self.cpu = cpu
        self.segment_name = ea.segment
        self.offset = ea.offset
        self.bits = bits
        self.text = f"{ea.segment}:{ea.text}"

    def read(self) -> int:
        segv = getattr(self.cpu.s, self.segment_name)
        return self.cpu.mem.rb(segv, self.offset) if self.bits == 8 else self.cpu.mem.rw(segv, self.offset)

    def write(self, value: int) -> None:
        segv = getattr(self.cpu.s, self.segment_name)
        if self.bits == 8:
            self.cpu.mem.wb(segv, self.offset, value)
        else:
            self.cpu.mem.ww(segv, self.offset, value)

    def read_far(self) -> tuple[int, int]:
        segv = getattr(self.cpu.s, self.segment_name)
        return self.cpu.mem.rw(segv, self.offset), self.cpu.mem.rw(segv, (self.offset + 2) & 0xFFFF)


@dataclass
class CPU8086:
    mem: Memory
    s: CPUState = field(default_factory=CPUState)
    halted: bool = False
    trace: list[str] = field(default_factory=list)
    trace_enabled: bool = True
    instruction_count: int = 0
    call_depth: int = 0
    interrupt_handler: Callable[["CPU8086", int], None] | None = None
    port_reader: Callable[["CPU8086", int, int], int] | None = None
    port_writer: Callable[["CPU8086", int, int, int], None] | None = None
    replacement_hooks: dict[tuple[int, int], Callable[["CPU8086"], None]] = field(default_factory=dict)
    hook_names: dict[tuple[int, int], str] = field(default_factory=dict)
    hook_verifier: Callable[["CPU8086", tuple[int, int], Callable[["CPU8086"], None], str], None] | None = None
    hook_verifier_passthrough: set[tuple[int, int]] = field(default_factory=set)
    # Optional live-side replacements used only while a differential hook
    # transaction is executing the replacement handler.  Interactive front-ends
    # use this to keep UI presenter/timer hooks publishing frames without letting
    # their normal frame-boundary exceptions interrupt the verified routine.
    hook_verifier_live_passthrough_overrides: dict[tuple[int, int], Callable[["CPU8086"], None]] = field(default_factory=dict)
    # Interactive front-ends sometimes need a publish/pacing boundary while a
    # verified parent hook is still running.  The live-side passthrough wrapper
    # cannot raise the normal UI boundary exception immediately, because that
    # would abort the differential transaction before the ASM-vs-hook diff is
    # computed.  Instead it sets this flag; HookVerifier raises the optional
    # callback after the verified hook has reached its continuation and compared
    # cleanly.
    hook_verifier_live_yield_requested: bool = False
    hook_verifier_live_yield_callback: Callable[[], None] | None = None
    # When a lifted parent executes an original bounded CALL or directly invokes
    # an installed child hook, keep differential verification active at the
    # nested hook boundary.  This makes child addresses real oracle checkpoints
    # instead of shared black boxes inside a larger parent transaction.
    hook_verifier_verify_nested_calls: bool = True
    # Optional real-time pacer invoked once per modelled timer tick (the game's
    # PIT/timer wait).  Left None for headless/deterministic runs; an interactive
    # front-end sets it to throttle the game to real time.
    timer_pacer: Callable[[], None] | None = None
    timer_ticks_elapsed: int = 0
    # Optional hardware-interrupt source (a PIC).  When set, it is polled at each
    # instruction boundary with IF set; returning an IRQ number delivers it inline
    # (real hardware-interrupt entry into the IVT handler).  Left None on the
    # deterministic demo/test path so that timing there is unchanged.
    pending_irq: "Callable[[], int | None] | None" = None
    max_rep_count: int = 1_000_000
    # Optional generic execution telemetry sink. The CPU emits only raw events;
    # game-specific island classification lives outside the interpreter.
    coverage_telemetry: Any | None = None

    def addr(self) -> tuple[int, int]:
        return self.s.cs & 0xFFFF, self.s.ip & 0xFFFF

    def set_flag(self, flag: int, value: bool) -> None:
        if value:
            self.s.flags |= flag
        else:
            self.s.flags &= ~flag
        self.s.flags |= 0x0002
        self.s.flags &= 0x0FFF

    def get_flag(self, flag: int) -> bool:
        return bool(self.s.flags & flag)

    def parity(self, value: int) -> bool:
        return _PARITY[value & 0xFF]

    # The three flag helpers below are extremely hot.  They compute the whole
    # flags word in one assignment instead of 5-6 set_flag() calls each.  The bits
    # they touch (and the ones they preserve) match the original set_flag-based
    # versions exactly; the regression suite checks the resulting flags.
    def set_logic_flags(self, result: int, bits: int) -> None:
        sign = 1 << (bits - 1)
        r = result & ((1 << bits) - 1)
        # Clear CF, PF, ZF, SF, OF (leave AF, like the original); CF=OF=0.
        f = self.s.flags & ~0x08C5
        if r == 0:
            f |= ZF
        if r & sign:
            f |= SF
        if _PARITY[r & 0xFF]:
            f |= PF
        self.s.flags = (f | 0x0002) & 0x0FFF

    def set_add_flags(self, a: int, b: int, result: int, bits: int, carry: int = 0) -> None:
        # ``a`` and ``b`` are the *original* operands; ``carry`` is the incoming
        # carry for ADC (0 for plain ADD).  ``result`` is the full unmasked
        # a+b+carry.  Folding carry into b before this call would destroy the
        # nibble-carry (AF) and sign (OF) information, so it is kept separate.
        mask = (1 << bits) - 1
        sign = 1 << (bits - 1)
        r = result & mask
        f = self.s.flags & ~0x08D5  # clear CF, PF, AF, ZF, SF, OF
        if result > mask:
            f |= CF
        if r == 0:
            f |= ZF
        if r & sign:
            f |= SF
        if _PARITY[r & 0xFF]:
            f |= PF
        if ((a & 0xF) + (b & 0xF) + carry) > 0xF:
            f |= AF
        if (~(a ^ b) & (a ^ r)) & sign:
            f |= OF
        self.s.flags = (f | 0x0002) & 0x0FFF

    def set_sub_flags(self, a: int, b: int, result: int, bits: int, carry: int = 0) -> None:
        # ``a`` and ``b`` are the *original* operands; ``carry`` is the incoming
        # borrow for SBB (0 for plain SUB/CMP).  ``result`` is the full signed
        # a-b-carry (may be negative).  CF is the true borrow (result < 0); AF and
        # OF use the original operands so the borrow does not corrupt them.
        mask = (1 << bits) - 1
        sign = 1 << (bits - 1)
        r = result & mask
        f = self.s.flags & ~0x08D5  # clear CF, PF, AF, ZF, SF, OF
        if result < 0:
            f |= CF
        if r == 0:
            f |= ZF
        if r & sign:
            f |= SF
        if _PARITY[r & 0xFF]:
            f |= PF
        if ((a & 0xF) - (b & 0xF) - carry) < 0:
            f |= AF
        if ((a ^ b) & (a ^ r)) & sign:
            f |= OF
        self.s.flags = (f | 0x0002) & 0x0FFF

    def get_reg16(self, idx: int) -> int:
        return getattr(self.s, REG16[idx]) & 0xFFFF

    def set_reg16(self, idx: int, value: int) -> None:
        setattr(self.s, REG16[idx], value & 0xFFFF)

    def get_reg8(self, idx: int) -> int:
        r = REG8[idx]
        base = r[0] + "x"
        v = getattr(self.s, base)
        return (v >> 8) & 0xFF if r[1] == "h" else v & 0xFF

    def set_reg8(self, idx: int, value: int) -> None:
        r = REG8[idx]
        base = r[0] + "x"
        cur = getattr(self.s, base)
        if r[1] == "h":
            cur = (cur & 0x00FF) | ((value & 0xFF) << 8)
        else:
            cur = (cur & 0xFF00) | (value & 0xFF)
        setattr(self.s, base, cur & 0xFFFF)

    def get_sreg(self, idx: int) -> int:
        return getattr(self.s, SREG[idx]) & 0xFFFF

    def set_sreg(self, idx: int, value: int) -> None:
        setattr(self.s, SREG[idx], value & 0xFFFF)

    def fetch8(self) -> int:
        v = self.mem.rb(self.s.cs, self.s.ip)
        self.s.ip = (self.s.ip + 1) & 0xFFFF
        return v

    def fetch16(self) -> int:
        lo = self.fetch8()
        hi = self.fetch8()
        return lo | (hi << 8)

    def push(self, value: int) -> None:
        self.s.sp = (self.s.sp - 2) & 0xFFFF
        self.mem.ww(self.s.ss, self.s.sp, value)

    def pop(self) -> int:
        v = self.mem.rw(self.s.ss, self.s.sp)
        self.s.sp = (self.s.sp + 2) & 0xFFFF
        return v

    def sign8(self, v: int) -> int:
        return v - 0x100 if v & 0x80 else v

    def sign16(self, v: int) -> int:
        return v - 0x10000 if v & 0x8000 else v

    def decode_ea(self, mod: int, rm: int, seg_override: str | None = None) -> EffectiveAddress:
        disp = 0
        if mod == 0 and rm == 6:
            disp = self.fetch16()
            base = 0
            text = f"[{disp:04X}]"
            default_seg = "ds"
        else:
            if rm == 0:
                base = self.s.bx + self.s.si; text = "[bx+si]"; default_seg = "ds"
            elif rm == 1:
                base = self.s.bx + self.s.di; text = "[bx+di]"; default_seg = "ds"
            elif rm == 2:
                base = self.s.bp + self.s.si; text = "[bp+si]"; default_seg = "ss"
            elif rm == 3:
                base = self.s.bp + self.s.di; text = "[bp+di]"; default_seg = "ss"
            elif rm == 4:
                base = self.s.si; text = "[si]"; default_seg = "ds"
            elif rm == 5:
                base = self.s.di; text = "[di]"; default_seg = "ds"
            elif rm == 6:
                base = self.s.bp; text = "[bp]"; default_seg = "ss"
            else:
                base = self.s.bx; text = "[bx]"; default_seg = "ds"
            if mod == 1:
                d = self.sign8(self.fetch8())
                disp = d
                text = text[:-1] + (f"{d:+d}]")
            elif mod == 2:
                d = self.fetch16()
                disp = self.sign16(d)
                text = text[:-1] + (f"{disp:+d}]")
        return EffectiveAddress(seg_override or default_seg, (base + disp) & 0xFFFF, text)


    def decode_rm_operand(self, mod: int, rm: int, bits: int, seg_override: str | None = None):
        if mod == 3:
            return _RegOperand(self, rm, bits)
        return _MemOperand(self, self.decode_ea(mod, rm, seg_override), bits)

    def read_rm(self, mod: int, rm: int, bits: int, seg_override: str | None = None) -> tuple[int, str]:
        if mod == 3:
            return (self.get_reg8(rm) if bits == 8 else self.get_reg16(rm)), (REG8[rm] if bits == 8 else REG16[rm])
        ea = self.decode_ea(mod, rm, seg_override)
        seg = getattr(self.s, ea.segment)
        v = self.mem.rb(seg, ea.offset) if bits == 8 else self.mem.rw(seg, ea.offset)
        return v, f"{ea.segment}:{ea.text}"

    def write_rm(self, mod: int, rm: int, bits: int, value: int, seg_override: str | None = None) -> str:
        if mod == 3:
            if bits == 8:
                self.set_reg8(rm, value)
                return REG8[rm]
            self.set_reg16(rm, value)
            return REG16[rm]
        ea = self.decode_ea(mod, rm, seg_override)
        seg = getattr(self.s, ea.segment)
        if bits == 8:
            self.mem.wb(seg, ea.offset, value)
        else:
            self.mem.ww(seg, ea.offset, value)
        return f"{ea.segment}:{ea.text}"

    def peek_modrm(self) -> tuple[int, int, int, int]:
        m = self.fetch8()
        return m, (m >> 6) & 3, (m >> 3) & 7, m & 7

    def _enter_hardware_interrupt(self, irq: int) -> None:
        """Real IRQ entry: push flags/cs/ip, clear IF/TF, jump to the IVT handler."""
        vec = (0x08 + irq) if irq < 8 else (0x70 + irq - 8)
        off = self.mem.rw(0, vec * 4)
        seg = self.mem.rw(0, vec * 4 + 2)
        self.push(self.s.flags)
        self.push(self.s.cs & 0xFFFF)
        self.push(self.s.ip & 0xFFFF)
        self.set_flag(IF, False)
        self.set_flag(TF, False)
        self.s.cs, self.s.ip = seg & 0xFFFF, off & 0xFFFF

    def step(self) -> None:
        if self.halted:
            raise HaltExecution()

        # Deliver a pending hardware interrupt at this instruction boundary.
        if self.pending_irq is not None and (self.s.flags & IF):
            irq = self.pending_irq()
            if irq is not None:
                self._enter_hardware_interrupt(irq)
                return

        start_cs, start_ip = self.s.cs & 0xFFFF, self.s.ip & 0xFFFF
        hook_key = (start_cs, start_ip)
        if hook_key in self.replacement_hooks:
            before = self.s.snapshot() if self.trace_enabled else ""
            name = self.hook_names.get(hook_key, "replacement")
            handler = self.replacement_hooks[hook_key]
            if self.hook_verifier is not None and hook_key not in self.hook_verifier_passthrough:
                self.hook_verifier(self, hook_key, handler, name)
            else:
                try:
                    handler(self)
                finally:
                    if self.coverage_telemetry is not None:
                        self.coverage_telemetry.record_hook_unverified(hook_key, name)
            self.instruction_count += 1
            if self.trace_enabled:
                self.trace.append(f"{start_cs:04X}:{start_ip:04X}  HOOK {name:<23} {before} -> {self.s.snapshot()}")
            return

        seg_override: str | None = None
        rep: int | None = None
        while True:
            op = self.fetch8()
            if op == 0x26 or op == 0x2E or op == 0x36 or op == 0x3E:
                seg_override = _SEG_OVERRIDE[op]
                continue
            if op == 0xF2 or op == 0xF3:
                rep = op
                continue
            if op == 0x66:
                # PRE2 contains a small 386 CPU-probe path using operand-size
                # prefixes (notably ``66 33 C0`` / xor eax,eax).  The VM is a
                # 16-bit source-recovery oracle, so we deliberately execute the
                # following instruction with the visible 16-bit register state.
                # That preserves the game-observable low word while avoiding a
                # full 80386 register model during bootstrap bring-up.
                continue
            break
        asm = self.execute_opcode(op, seg_override, rep)
        if self.coverage_telemetry is not None:
            self.coverage_telemetry.record_interpreted_instruction((start_cs, start_ip))
        self.instruction_count += 1
        if self.trace_enabled:
            self.trace.append(f"{start_cs:04X}:{start_ip:04X}  d{self.call_depth:02d} {asm:<34} {self.s.snapshot()}")

    def run(self, max_steps: int = 1000) -> int:
        steps = 0
        while steps < max_steps and not self.halted:
            self.step()
            steps += 1
        return steps

    def execute_opcode(self, op: int, seg_override: str | None, rep: int | None) -> str:
        s = self.s
        # MOV immediate to register
        if 0xB0 <= op <= 0xB7:
            reg = op - 0xB0; imm = self.fetch8(); self.set_reg8(reg, imm); return f"mov {REG8[reg]},{imm:02X}h"
        if 0xB8 <= op <= 0xBF:
            reg = op - 0xB8; imm = self.fetch16(); self.set_reg16(reg, imm); return f"mov {REG16[reg]},{imm:04X}h"

        # PUSH/POP registers and segment registers
        if 0x50 <= op <= 0x57:
            reg = op - 0x50; self.push(self.get_reg16(reg)); return f"push {REG16[reg]}"
        if 0x58 <= op <= 0x5F:
            reg = op - 0x58; self.set_reg16(reg, self.pop()); return f"pop {REG16[reg]}"
        if op in (0x06, 0x0E, 0x16, 0x1E):
            idx = {0x06: 0, 0x0E: 1, 0x16: 2, 0x1E: 3}[op]; self.push(self.get_sreg(idx)); return f"push {SREG[idx]}"
        if op in (0x07, 0x17, 0x1F):
            idx = {0x07: 0, 0x17: 2, 0x1F: 3}[op]; self.set_sreg(idx, self.pop()); return f"pop {SREG[idx]}"
        if op == 0x68:
            imm = self.fetch16()
            self.push(imm)
            return f"push {imm:04X}h"
        if op == 0x6A:
            imm8 = self.fetch8()
            imm = imm8 | 0xFF00 if imm8 & 0x80 else imm8
            self.push(imm)
            return f"push {imm:04X}h"
        if op == 0x9C:
            self.push(s.flags); return "pushf"
        if op == 0x9D:
            s.flags = self.pop() | 0x0002; return "popf"
        if op == 0x98:
            al = s.ax & 0x00FF
            s.ax = al | (0xFF00 if al & 0x80 else 0x0000)
            return "cbw"

        # MOV between r/m and reg / segment
        if op in (0x88, 0x89, 0x8A, 0x8B):
            bits = 8 if op in (0x88, 0x8A) else 16
            _, mod, reg, rm = self.peek_modrm()
            operand = self.decode_rm_operand(mod, rm, bits, seg_override)
            if op in (0x88, 0x89):
                val = self.get_reg8(reg) if bits == 8 else self.get_reg16(reg)
                operand.write(val)
                return f"mov {operand.text},{REG8[reg] if bits == 8 else REG16[reg]}"
            val = operand.read()
            if bits == 8: self.set_reg8(reg, val)
            else: self.set_reg16(reg, val)
            return f"mov {REG8[reg] if bits == 8 else REG16[reg]},{operand.text}"
        if op == 0x8C:
            _, mod, reg, rm = self.peek_modrm(); operand = self.decode_rm_operand(mod, rm, 16, seg_override); operand.write(self.get_sreg(reg & 3)); return f"mov {operand.text},{SREG[reg & 3]}"
        if op == 0x8E:
            _, mod, reg, rm = self.peek_modrm(); operand = self.decode_rm_operand(mod, rm, 16, seg_override); val = operand.read(); self.set_sreg(reg & 3, val); return f"mov {SREG[reg & 3]},{operand.text}"
        if op in (0xA0, 0xA1, 0xA2, 0xA3):
            off = self.fetch16(); seg = getattr(s, seg_override or "ds")
            if op == 0xA0:
                s.ax = (s.ax & 0xFF00) | self.mem.rb(seg, off); return f"mov al,[{off:04X}]"
            if op == 0xA1:
                s.ax = self.mem.rw(seg, off); return f"mov ax,[{off:04X}]"
            if op == 0xA2:
                self.mem.wb(seg, off, s.ax); return f"mov [{off:04X}],al"
            self.mem.ww(seg, off, s.ax); return f"mov [{off:04X}],ax"
        if op in (0xC6, 0xC7):
            bits = 8 if op == 0xC6 else 16
            _, mod, reg, rm = self.peek_modrm()
            if reg != 0: raise UnsupportedInstruction(f"group mov /{reg} at {s.cs:04X}:{(s.ip-2)&0xffff:04X}")
            operand = self.decode_rm_operand(mod, rm, bits, seg_override)
            imm = self.fetch8() if bits == 8 else self.fetch16()
            operand.write(imm)
            return f"mov {operand.text},{imm:0{bits//4}X}h"

        # Arithmetic accumulator immediates
        if op in (0x04,0x05,0x0C,0x0D,0x14,0x15,0x1C,0x1D,0x24,0x25,0x2C,0x2D,0x34,0x35,0x3C,0x3D):
            bits = 8 if op % 2 == 0 else 16
            imm = self.fetch8() if bits == 8 else self.fetch16()
            a = self.get_reg8(0) if bits == 8 else s.ax
            group = (op >> 3) & 7
            res, name = self.alu(group, a, imm, bits, write=False)
            if group != 7:
                if bits == 8: self.set_reg8(0, res)
                else: s.ax = res & 0xFFFF
            return f"{name} {'al' if bits == 8 else 'ax'},{imm:0{bits//4}X}h"

        # Arithmetic r/m with reg, directions 00-03,08-0B,10-13,18-1B,20-23,28-2B,30-33,38-3B
        if op < 0x40 and (op & 0x04) == 0 and (op & 0x07) in (0,1,2,3):
            group = (op >> 3) & 7
            bits = 8 if (op & 1) == 0 else 16
            direction_to_reg = bool(op & 2)
            _, mod, reg, rm = self.peek_modrm()
            operand = self.decode_rm_operand(mod, rm, bits, seg_override)
            rmv = operand.read()
            regv = self.get_reg8(reg) if bits == 8 else self.get_reg16(reg)
            if direction_to_reg:
                res, name = self.alu(group, regv, rmv, bits, write=False)
                if group != 7:
                    if bits == 8: self.set_reg8(reg, res)
                    else: self.set_reg16(reg, res)
                return f"{name} {REG8[reg] if bits==8 else REG16[reg]},{operand.text}"
            res, name = self.alu(group, rmv, regv, bits, write=False)
            if group != 7:
                operand.write(res)
            return f"{name} {operand.text},{REG8[reg] if bits==8 else REG16[reg]}"

        # Group 1 immediate to r/m
        if op in (0x80, 0x81, 0x82, 0x83):
            bits = 8 if op in (0x80, 0x82) else 16
            _, mod, reg, rm = self.peek_modrm()
            operand = self.decode_rm_operand(mod, rm, bits, seg_override)
            dstv = operand.read()
            if op == 0x83:
                imm = self.sign8(self.fetch8()) & 0xFFFF
            else:
                imm = self.fetch8() if bits == 8 else self.fetch16()
            res, name = self.alu(reg, dstv, imm, bits, write=False)
            if reg != 7:
                operand.write(res)
            return f"{name} {operand.text},{imm:0{bits//4}X}h"

        # INC/DEC registers and r/m
        if 0x40 <= op <= 0x47:
            reg = op - 0x40; old = self.get_reg16(reg); res = (old + 1) & 0xFFFF; old_cf = self.get_flag(CF); self.set_add_flags(old,1,old+1,16); self.set_flag(CF, old_cf); self.set_reg16(reg,res); return f"inc {REG16[reg]}"
        if 0x48 <= op <= 0x4F:
            reg = op - 0x48; old = self.get_reg16(reg); res = (old - 1) & 0xFFFF; old_cf = self.get_flag(CF); self.set_sub_flags(old,1,old-1,16); self.set_flag(CF, old_cf); self.set_reg16(reg,res); return f"dec {REG16[reg]}"
        if op in (0xFE, 0xFF):
            bits = 8 if op == 0xFE else 16
            _, mod, reg, rm = self.peek_modrm()
            if reg in (0,1):
                operand = self.decode_rm_operand(mod, rm, bits, seg_override)
                old = operand.read()
                if reg == 0:
                    res = (old + 1) & ((1 << bits)-1); old_cf = self.get_flag(CF); self.set_add_flags(old,1,old+1,bits); self.set_flag(CF, old_cf); opn = "inc"
                else:
                    res = (old - 1) & ((1 << bits)-1); old_cf = self.get_flag(CF); self.set_sub_flags(old,1,old-1,bits); self.set_flag(CF, old_cf); opn = "dec"
                operand.write(res); return f"{opn} {operand.text}"
            if op == 0xFF and reg == 2:
                operand = self.decode_rm_operand(mod, rm, 16, seg_override); target = operand.read(); self.push(s.ip); s.ip = target; return f"call {operand.text}"
            if op == 0xFF and reg == 3:
                if mod == 3:
                    raise UnsupportedInstruction("far indirect call requires memory operand")
                operand = self.decode_rm_operand(mod, rm, 16, seg_override)
                off, farseg = operand.read_far()
                self.push(s.cs); self.push(s.ip); s.cs = farseg; s.ip = off
                return f"call far {operand.text}"
            if op == 0xFF and reg == 4:
                operand = self.decode_rm_operand(mod, rm, 16, seg_override); target = operand.read(); s.ip = target; return f"jmp {operand.text}"
            if op == 0xFF and reg == 5:
                if mod == 3:
                    raise UnsupportedInstruction("far indirect jmp requires memory operand")
                operand = self.decode_rm_operand(mod, rm, 16, seg_override)
                off, farseg = operand.read_far()
                s.cs = farseg; s.ip = off
                return f"jmp far {operand.text} -> {farseg:04X}:{off:04X}"
            if op == 0xFF and reg == 6:
                operand = self.decode_rm_operand(mod, rm, 16, seg_override); val = operand.read(); self.push(val); return f"push {operand.text}"
            raise UnsupportedInstruction(f"group FE/FF /{reg}")

        # TEST/XCHG/LEA
        if op in (0x84, 0x85):
            bits = 8 if op == 0x84 else 16; _, mod, reg, rm = self.peek_modrm(); operand = self.decode_rm_operand(mod, rm, bits, seg_override); a = operand.read(); b = self.get_reg8(reg) if bits == 8 else self.get_reg16(reg); self.set_logic_flags(a & b,bits); return f"test {operand.text},{REG8[reg] if bits==8 else REG16[reg]}"
        if op in (0xA8, 0xA9):
            bits = 8 if op == 0xA8 else 16; imm = self.fetch8() if bits==8 else self.fetch16(); a = self.get_reg8(0) if bits==8 else s.ax; self.set_logic_flags(a & imm,bits); return f"test {'al' if bits==8 else 'ax'},{imm:X}h"
        if op == 0x8D:
            _, mod, reg, rm = self.peek_modrm()
            if mod == 3: raise UnsupportedInstruction("lea with register source")
            ea = self.decode_ea(mod, rm, seg_override); self.set_reg16(reg, ea.offset); return f"lea {REG16[reg]},{ea.text}"
        if op in (0xC4, 0xC5):
            _, mod, reg, rm = self.peek_modrm()
            if mod == 3:
                raise UnsupportedInstruction("les/lds requires memory source")
            operand = self.decode_rm_operand(mod, rm, 16, seg_override)
            off, seg = operand.read_far()
            self.set_reg16(reg, off)
            if op == 0xC4:
                s.es = seg
                return f"les {REG16[reg]},{operand.text} -> {seg:04X}:{off:04X}"
            s.ds = seg
            return f"lds {REG16[reg]},{operand.text} -> {seg:04X}:{off:04X}"
        if op == 0x8F:  # POP r/m16.  The 8086 ignores the reg field (it is not a
            # real opcode group); some code emits non-zero reg bits and still pops.
            _, mod, reg, rm = self.peek_modrm()
            operand = self.decode_rm_operand(mod, rm, 16, seg_override)
            operand.write(self.pop())
            return f"pop {operand.text}"
        if op in (0x86, 0x87):
            bits = 8 if op == 0x86 else 16
            _, mod, reg, rm = self.peek_modrm(); operand = self.decode_rm_operand(mod, rm, bits, seg_override)
            if bits == 8:
                a = self.get_reg8(reg); b = operand.read(); self.set_reg8(reg, b); operand.write(a); return f"xchg {operand.text},{REG8[reg]}"
            a = self.get_reg16(reg); b = operand.read(); self.set_reg16(reg, b); operand.write(a); return f"xchg {operand.text},{REG16[reg]}"
        if 0x90 <= op <= 0x97:
            reg = op - 0x90
            if reg:
                ax = s.ax; s.ax = self.get_reg16(reg); self.set_reg16(reg, ax)
                return f"xchg ax,{REG16[reg]}"
            return "nop"

        # Control flow
        if op == 0xE8:
            rel = self.sign16(self.fetch16())
            ret = s.ip
            target = (s.ip + rel) & 0xFFFF
            self.push(ret)
            s.ip = target
            self.call_depth += 1
            return f"call near -> {s.cs:04X}:{target:04X} ret={ret:04X}"
        if op == 0x9A:
            off = self.fetch16(); seg = self.fetch16(); ret_cs, ret_ip = s.cs, s.ip
            self.push(ret_cs); self.push(ret_ip); s.cs = seg; s.ip = off
            self.call_depth += 1
            return f"call far -> {seg:04X}:{off:04X} ret={ret_cs:04X}:{ret_ip:04X}"
        if op == 0xE9:
            rel = self.sign16(self.fetch16()); target = (s.ip + rel) & 0xFFFF; s.ip = target; return f"jmp near -> {s.cs:04X}:{target:04X}"
        if op == 0xEB:
            rel = self.sign8(self.fetch8()); target = (s.ip + rel) & 0xFFFF; s.ip = target; return f"jmp short -> {s.cs:04X}:{target:04X}"
        if op == 0xEA:
            off = self.fetch16(); seg = self.fetch16(); s.cs = seg; s.ip = off; return f"jmp far -> {seg:04X}:{off:04X}"
        if op == 0xC3:
            target = self.pop(); s.ip = target; self.call_depth = max(0, self.call_depth - 1); return f"ret near -> {s.cs:04X}:{target:04X}"
        if op == 0xC2:
            n = self.fetch16(); target = self.pop(); s.ip = target; s.sp = (s.sp + n) & 0xFFFF; self.call_depth = max(0, self.call_depth - 1); return f"ret near {n} -> {s.cs:04X}:{target:04X}"
        if op == 0xCB:
            target_ip = self.pop(); target_cs = self.pop(); s.ip = target_ip; s.cs = target_cs; self.call_depth = max(0, self.call_depth - 1); return f"ret far -> {target_cs:04X}:{target_ip:04X}"
        if op == 0xCA:
            n = self.fetch16(); target_ip = self.pop(); target_cs = self.pop(); s.ip = target_ip; s.cs = target_cs; s.sp = (s.sp + n) & 0xFFFF; self.call_depth = max(0, self.call_depth - 1); return f"ret far {n} -> {target_cs:04X}:{target_ip:04X}"
        if op == 0xCF:  # IRET: pop IP, CS, FLAGS
            target_ip = self.pop(); target_cs = self.pop(); s.flags = self.pop() | 0x0002
            s.ip = target_ip; s.cs = target_cs; self.call_depth = max(0, self.call_depth - 1)
            return f"iret -> {target_cs:04X}:{target_ip:04X}"
        if 0x70 <= op <= 0x7F:
            rel = self.sign8(self.fetch8()); take = self.condition(op & 0xF)
            old = s.ip
            if take: s.ip = (s.ip + rel) & 0xFFFF
            return f"{JCC_NAMES[op & 0xF]} -> {s.cs:04X}:{s.ip if take else old:04X} {'taken' if take else 'not'}"
        if op in (0xE0, 0xE1, 0xE2):
            rel = self.sign8(self.fetch8()); s.cx = (s.cx - 1) & 0xFFFF
            take = s.cx != 0 and (op == 0xE2 or (op == 0xE1 and self.get_flag(ZF)) or (op == 0xE0 and not self.get_flag(ZF)))
            target = (s.ip + rel) & 0xFFFF
            if take: s.ip = target
            name = {0xE0: 'loopne', 0xE1: 'loope', 0xE2: 'loop'}[op]
            return f"{name} -> {s.cs:04X}:{s.ip:04X} {'taken' if take else 'not'} cx={s.cx:04X}"
        if op == 0xE3:
            rel = self.sign8(self.fetch8()); take = s.cx == 0
            target = (s.ip + rel) & 0xFFFF
            if take: s.ip = target
            return f"jcxz -> {s.cs:04X}:{s.ip:04X} {'taken' if take else 'not'}"

        # String operations
        if op in (0x6C, 0x6D, 0x6E, 0x6F, 0xA4, 0xA5, 0xA6, 0xA7, 0xAA, 0xAB, 0xAC, 0xAD, 0xAE, 0xAF):
            return self.string_op(op, rep, seg_override)

        if op == 0xD7:  # XLAT
            seg = getattr(s, seg_override or "ds")
            off = (s.bx + (s.ax & 0xFF)) & 0xFFFF
            self.set_reg8(0, self.mem.rb(seg, off))
            return f"xlat {seg_override or 'ds'}:[bx+al]"

        if op == 0x27:  # DAA - decimal adjust AL after BCD addition
            old_al = self.get_reg8(0)
            old_cf = self.get_flag(CF)
            al = old_al
            adjust_low = (al & 0x0F) > 9 or self.get_flag(AF)
            if adjust_low:
                al = (al + 0x06) & 0xFF
            adjust_high = old_al > 0x99 or old_cf
            if adjust_high:
                al = (al + 0x60) & 0xFF
            self.set_reg8(0, al)
            self.set_flag(AF, adjust_low)
            self.set_flag(CF, adjust_high)
            # 8086 defines SF/ZF/PF from adjusted AL. OF is undefined; leave it
            # unchanged so code that does not rely on undefined OF remains stable.
            self.set_flag(ZF, al == 0)
            self.set_flag(SF, bool(al & 0x80))
            self.set_flag(PF, self.parity(al))
            return "daa"

        # Shift/rotate group 2
        if op in (0xC0,0xC1,0xD0,0xD1,0xD2,0xD3):
            bits = 8 if op in (0xC0,0xD0,0xD2) else 16
            _, mod, reg, rm = self.peek_modrm()
            operand = self.decode_rm_operand(mod, rm, bits, seg_override)
            if op in (0xD0,0xD1):
                count = 1
            elif op in (0xD2,0xD3):
                count = s.cx & 0xFF
            else:
                count = self.fetch8()
            val = operand.read()
            res = self.shift(reg, val, count, bits)
            operand.write(res)
            return f"shift/{reg} {operand.text},{count}"

        # Flag and misc
        if op == 0xF8: self.set_flag(CF, False); return "clc"
        if op == 0xF9: self.set_flag(CF, True); return "stc"
        if op == 0xFC: self.set_flag(DF, False); return "cld"
        if op == 0xFD: self.set_flag(DF, True); return "std"
        if op == 0xFA: self.set_flag(IF, False); return "cli"
        if op == 0xFB: self.set_flag(IF, True); return "sti"
        if op == 0xF4: self.halted = True; return "hlt"
        if op in (0xE4, 0xE5, 0xEC, 0xED):
            if op in (0xE4, 0xE5):
                port = self.fetch8()
            else:
                port = s.dx
            bits = 8 if op in (0xE4, 0xEC) else 16
            value = self.port_reader(self, port & 0xFFFF, bits) if self.port_reader else 0
            if bits == 8:
                self.set_reg8(0, value)
                return f"in al,{port:04X}h -> {value & 0xFF:02X}h"
            s.ax = value & 0xFFFF
            return f"in ax,{port:04X}h -> {value & 0xFFFF:04X}h"
        if op in (0xE6, 0xE7, 0xEE, 0xEF):
            if op in (0xE6, 0xE7):
                port = self.fetch8()
            else:
                port = s.dx
            bits = 8 if op in (0xE6, 0xEE) else 16
            value = self.get_reg8(0) if bits == 8 else s.ax
            if self.port_writer:
                self.port_writer(self, port & 0xFFFF, value, bits)
            return f"out {port:04X}h,{'al' if bits == 8 else 'ax'} ({value:0{bits//4}X}h)"
        if op == 0xCD:
            num = self.fetch8()
            before_ax = s.ax
            if self.interrupt_handler:
                self.interrupt_handler(self, num)
            else:
                raise UnsupportedInstruction(f"INT {num:02X}h not hooked")
            cf = 1 if self.get_flag(CF) else 0
            return f"int {num:02X}h ah={(before_ax >> 8) & 0xFF:02X}h ax:{before_ax:04X}->{s.ax:04X} cf={cf}"
        if op == 0xCC:
            if self.interrupt_handler: self.interrupt_handler(self, 3)
            return "int3"

        # Group 3 unary
        if op in (0xF6,0xF7):
            bits = 8 if op == 0xF6 else 16
            _, mod, reg, rm = self.peek_modrm(); operand = self.decode_rm_operand(mod,rm,bits,seg_override); val = operand.read()
            if reg == 0:
                imm = self.fetch8() if bits == 8 else self.fetch16(); self.set_logic_flags(val & imm,bits); return f"test {operand.text},{imm:X}h"
            if reg == 2:
                operand.write((~val)&((1<<bits)-1)); return f"not {operand.text}"
            if reg == 3:
                res = (-val) & ((1<<bits)-1); self.set_sub_flags(0,val,-val,bits); operand.write(res); return f"neg {operand.text}"
            if reg == 4:  # MUL unsigned
                if bits == 8:
                    result = (s.ax & 0x00FF) * (val & 0xFF)
                    s.ax = result & 0xFFFF
                    carry = (result >> 8) != 0
                else:
                    result = (s.ax & 0xFFFF) * (val & 0xFFFF)
                    s.ax = result & 0xFFFF
                    s.dx = (result >> 16) & 0xFFFF
                    carry = s.dx != 0
                self.set_flag(CF, carry); self.set_flag(OF, carry)
                return f"mul {operand.text}"
            if reg == 5:  # IMUL signed
                if bits == 8:
                    a = self.sign8(s.ax & 0xFF); b = self.sign8(val & 0xFF); result = a * b
                    s.ax = result & 0xFFFF
                    carry = not (-128 <= result <= 127)
                else:
                    a = self.sign16(s.ax); b = self.sign16(val); result = a * b
                    s.ax = result & 0xFFFF
                    s.dx = (result >> 16) & 0xFFFF
                    carry = not (-32768 <= result <= 32767)
                self.set_flag(CF, carry); self.set_flag(OF, carry)
                return f"imul {operand.text}"
            if reg == 6:  # DIV unsigned
                if val == 0:
                    raise ZeroDivisionError(f"div by zero at {s.cs:04X}:{s.ip:04X}")
                if bits == 8:
                    dividend = s.ax & 0xFFFF
                    q, r = divmod(dividend, val & 0xFF)
                    if q > 0xFF: raise OverflowError("8-bit div quotient overflow")
                    s.ax = ((r & 0xFF) << 8) | (q & 0xFF)
                else:
                    dividend = ((s.dx & 0xFFFF) << 16) | (s.ax & 0xFFFF)
                    q, r = divmod(dividend, val & 0xFFFF)
                    if q > 0xFFFF: raise OverflowError("16-bit div quotient overflow")
                    s.ax = q & 0xFFFF; s.dx = r & 0xFFFF
                return f"div {operand.text}"
            if reg == 7:  # IDIV signed
                if val == 0:
                    raise ZeroDivisionError(f"idiv by zero at {s.cs:04X}:{s.ip:04X}")
                if bits == 8:
                    dividend = self.sign16(s.ax)
                    divisor = self.sign8(val & 0xFF)
                    q = int(dividend / divisor); r = dividend - q * divisor
                    if q < -128 or q > 127: raise OverflowError("8-bit idiv quotient overflow")
                    s.ax = ((r & 0xFF) << 8) | (q & 0xFF)
                else:
                    dividend = ((s.dx & 0xFFFF) << 16) | (s.ax & 0xFFFF)
                    if dividend & 0x80000000: dividend -= 0x100000000
                    divisor = self.sign16(val & 0xFFFF)
                    q = int(dividend / divisor); r = dividend - q * divisor
                    if q < -32768 or q > 32767: raise OverflowError("16-bit idiv quotient overflow")
                    s.ax = q & 0xFFFF; s.dx = r & 0xFFFF
                return f"idiv {operand.text}"
            raise UnsupportedInstruction(f"group F6/F7 /{reg}")

        raise UnsupportedInstruction(f"Unsupported opcode {op:02X} at {s.cs:04X}:{(s.ip-1)&0xFFFF:04X}")

    def alu(self, group: int, a: int, b: int, bits: int, write: bool) -> tuple[int, str]:
        mask = (1 << bits) - 1
        names = ["add", "or", "adc", "sbb", "and", "sub", "xor", "cmp"]
        if group == 0:
            res = a + b; self.set_add_flags(a,b,res,bits)
        elif group == 1:
            res = a | b; self.set_logic_flags(res,bits)
        elif group == 2:
            carry = 1 if self.get_flag(CF) else 0; res = a + b + carry; self.set_add_flags(a,b,res,bits,carry)
        elif group == 3:
            carry = 1 if self.get_flag(CF) else 0; res = a - b - carry; self.set_sub_flags(a,b,res,bits,carry)
        elif group == 4:
            res = a & b; self.set_logic_flags(res,bits)
        elif group == 5:
            res = a - b; self.set_sub_flags(a,b,res,bits)
        elif group == 6:
            res = a ^ b; self.set_logic_flags(res,bits)
        elif group == 7:
            res = a - b; self.set_sub_flags(a,b,res,bits)
        else:
            raise AssertionError(group)
        return res & mask, names[group]

    def condition(self, cond: int) -> bool:
        cf,zf,sf,of,pf = self.get_flag(CF),self.get_flag(ZF),self.get_flag(SF),self.get_flag(OF),self.get_flag(PF)
        return [of, not of, cf, not cf, zf, not zf, cf or zf, not(cf or zf), sf, not sf, pf, not pf, sf != of, sf == of, zf or (sf != of), (not zf) and (sf == of)][cond]

    def string_op(self, op: int, rep: int | None, seg_override: str | None = None) -> str:
        s = self.s
        width = 2 if op in (0x6D,0x6F,0xA5,0xA7,0xAB,0xAD,0xAF) else 1
        delta = -width if self.get_flag(DF) else width
        count = s.cx if rep is not None else 1
        if count > self.max_rep_count:
            raise RuntimeError(f"REP count too large: {count}")
        done = 0
        while count > 0:
            done += 1
            if op in (0x6C,0x6D):
                value = self.port_reader(self, s.dx & 0xFFFF, 8 if width == 1 else 16) if self.port_reader else 0
                if width == 1: self.mem.wb(s.es, s.di, value)
                else: self.mem.ww(s.es, s.di, value)
                s.di = (s.di + delta) & 0xFFFF
            elif op in (0x6E,0x6F):
                src_seg = getattr(s, seg_override or "ds")
                value = self.mem.rb(src_seg, s.si) if width == 1 else self.mem.rw(src_seg, s.si)
                if self.port_writer:
                    self.port_writer(self, s.dx & 0xFFFF, value, 8 if width == 1 else 16)
                s.si = (s.si + delta) & 0xFFFF
            elif op in (0xA4,0xA5):
                src_seg = getattr(s, seg_override or "ds")
                val = self.mem.rb(src_seg, s.si) if width == 1 else self.mem.rw(src_seg, s.si)
                if width == 1: self.mem.wb(s.es, s.di, val)
                else: self.mem.ww(s.es, s.di, val)
                s.si = (s.si + delta) & 0xFFFF; s.di = (s.di + delta) & 0xFFFF
            elif op in (0xA6,0xA7):
                src_seg = getattr(s, seg_override or "ds")
                left = self.mem.rb(src_seg, s.si) if width == 1 else self.mem.rw(src_seg, s.si)
                right = self.mem.rb(s.es, s.di) if width == 1 else self.mem.rw(s.es, s.di)
                self.set_sub_flags(left, right, left - right, 8 if width == 1 else 16)
                s.si = (s.si + delta) & 0xFFFF
                s.di = (s.di + delta) & 0xFFFF
                if rep is not None:
                    s.cx = (s.cx - 1) & 0xFFFF
                    count -= 1
                    if rep == 0xF3 and not self.get_flag(ZF):
                        break
                    if rep == 0xF2 and self.get_flag(ZF):
                        break
                    continue
            elif op in (0xAA,0xAB):
                if width == 1: self.mem.wb(s.es, s.di, s.ax)
                else: self.mem.ww(s.es, s.di, s.ax)
                s.di = (s.di + delta) & 0xFFFF
            elif op in (0xAC,0xAD):
                src_seg = getattr(s, seg_override or "ds")
                if width == 1: self.set_reg8(0, self.mem.rb(src_seg, s.si))
                else: s.ax = self.mem.rw(src_seg, s.si)
                s.si = (s.si + delta) & 0xFFFF
            elif op in (0xAE,0xAF):
                memv = self.mem.rb(s.es, s.di) if width == 1 else self.mem.rw(s.es, s.di)
                acc = self.get_reg8(0) if width == 1 else s.ax
                self.set_sub_flags(acc, memv, acc - memv, 8 if width == 1 else 16)
                s.di = (s.di + delta) & 0xFFFF
                if rep is not None:
                    s.cx = (s.cx - 1) & 0xFFFF
                    count -= 1
                    if rep == 0xF3 and not self.get_flag(ZF):
                        break
                    if rep == 0xF2 and self.get_flag(ZF):
                        break
                    continue
            if rep is not None:
                s.cx = (s.cx - 1) & 0xFFFF
            count -= 1
            if rep is None:
                break
        names = {
            0x6C:"insb",0x6D:"insw",0x6E:"outsb",0x6F:"outsw",
            0xA4:"movsb",0xA5:"movsw",0xA6:"cmpsb",0xA7:"cmpsw",0xAA:"stosb",0xAB:"stosw",
            0xAC:"lodsb",0xAD:"lodsw",0xAE:"scasb",0xAF:"scasw",
        }
        return ("rep " if rep else "") + names[op] + (f" ; {done}" if rep else "")

    def shift(self, group: int, val: int, count: int, bits: int) -> int:
        mask = (1 << bits) - 1
        count &= 0x1F
        res = val & mask
        if count == 0:
            return res
        orig = res
        is_rotate = group in (0, 1, 2, 3)
        for _ in range(count):
            if group == 4:  # shl/sal
                self.set_flag(CF, bool(res & (1 << (bits-1))))
                res = (res << 1) & mask
            elif group == 5:  # shr
                self.set_flag(CF, bool(res & 1))
                res >>= 1
            elif group == 7:  # sar
                self.set_flag(CF, bool(res & 1))
                res = (res >> 1) | (res & (1 << (bits-1)))
            elif group == 0:  # rol
                c = bool(res & (1 << (bits-1))); res = ((res << 1) & mask) | (1 if c else 0); self.set_flag(CF,c)
            elif group == 1:  # ror
                c = bool(res & 1); res = (res >> 1) | ((1 << (bits-1)) if c else 0); self.set_flag(CF,c)
            elif group == 2:  # rcl
                old_cf = 1 if self.get_flag(CF) else 0
                new_cf = bool(res & (1 << (bits-1)))
                res = ((res << 1) & mask) | old_cf
                self.set_flag(CF, new_cf)
            elif group == 3:  # rcr
                old_cf = 1 if self.get_flag(CF) else 0
                new_cf = bool(res & 1)
                res = (res >> 1) | (old_cf << (bits-1))
                self.set_flag(CF, new_cf)
            else:
                raise UnsupportedInstruction(f"unsupported shift group /{group}")
        # Rotates only define CF and, for count=1, OF. They do not update ZF/SF/PF.
        # The PRE2 SQZ decoder relies on CF flowing through SHR/RCL chains, so
        # touching the normal arithmetic flags here corrupts compressed streams.
        if not is_rotate:
            self.set_flag(ZF, res == 0); self.set_flag(SF, bool(res & (1 << (bits-1)))); self.set_flag(PF, self.parity(res))
        # OF is only architecturally defined for a 1-bit shift/rotate (undefined for
        # other counts, so we leave it untouched there).  Drives JO/JG/JL/JGE/JLE.
        if count == 1:
            msb = (res >> (bits - 1)) & 1
            if group == 4:      # shl/sal: OF = CF(orig msb) ^ new msb
                of = ((orig >> (bits - 1)) & 1) ^ msb
            elif group == 5:    # shr: OF = original msb
                of = (orig >> (bits - 1)) & 1
            elif group == 7:    # sar: OF always 0
                of = 0
            elif group == 0:    # rol: OF = msb ^ new lsb (= new CF)
                of = msb ^ (res & 1)
            elif group == 2:    # rcl: OF = msb ^ new CF
                of = msb ^ (1 if self.get_flag(CF) else 0)
            else:               # ror / rcr: OF = top two bits of result differ
                of = msb ^ ((res >> (bits - 2)) & 1)
            self.set_flag(OF, bool(of))
        return res & mask

    def last_ea_offset(self, mod: int, rm: int) -> int:
        # Not reliable after decoding, placeholder for far indirect implementation work.
        raise UnsupportedInstruction("far indirect call/jmp EA reread not implemented")
