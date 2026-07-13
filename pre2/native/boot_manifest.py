"""Untangling the boot-data blackbox — what the 64 KB initial DGROUP (pre2/native/boot_data.py) actually is.

``boot_data.build_boot_memory()`` returns one opaque 64 KB blob (the program's linked DATA segment). This
module is the LEGEND for it: it decodes the genuinely meaningful tables into real Python data (the asset
filename manifest, the keyboard scancode map) and documents every non-zero region by name + kind, so the blob
reads as a structured table-of-contents instead of magic bytes. It is a pure INTERPRETATION over
``build_boot_memory()`` — it stores no bytes of its own, and ``verify_boot_manifest()`` proves every decoded
table re-encodes to the boot image byte-for-byte (lossless). Regions that are bitmap graphics or opaque lookup
tables are named + described but left as bytes (they are data, not code); decoding those to typed structures is
incremental follow-on.
"""
from __future__ import annotations

from dataclasses import dataclass

from pre2.native.boot_data import build_boot_memory

DS_BASE = 0x1A0F << 4


def _dgroup() -> bytes:
    return bytes(build_boot_memory()[DS_BASE:DS_BASE + 0x10000])


# --- the meaningful tables, decoded to real data --------------------------------------------------------------

@dataclass(frozen=True)
class ResourceRecord:
    """One entry of the boot resource table: a NUL-terminated record at ``offset``. Most are asset filenames
    (``*.SQZ`` graphics packs / ``*.TRK`` SoundTracker modules); a few are ``$``-terminated DOS error strings
    stored inline (e.g. the out-of-memory message)."""
    offset: int
    text: str

    @property
    def is_asset(self) -> bool:
        return self.text.endswith((".SQZ", ".TRK"))


def resource_table() -> list[ResourceRecord]:
    """[DGROUP 0x000C, after the 12-byte ``rep movsb`` boot stub] the NUL-terminated resource/message table —
    the game's asset manifest + a couple of inline DOS error strings."""
    dg = _dgroup()
    out: list[ResourceRecord] = []
    i = 0x0C
    while i < 0x0AC8:
        if dg[i] == 0:
            i += 1
            continue
        j = i
        while j < 0x0AC8 and dg[j] != 0:
            j += 1
        seg = dg[i:j]
        if all(32 <= c < 127 for c in seg):
            out.append(ResourceRecord(i, seg.decode()))
        i = j + 1
    return out


def scancode_char_table() -> str:
    """[DGROUP 0x2301] the keyboard scancode -> character map the password/menu entry uses (``'-'`` = no char);
    index the string by the make-code to get the typed character."""
    dg = _dgroup()
    return dg[0x2301:0x2301 + 0x54].decode("latin1")


def _s8(b: int) -> int:
    return b - 0x100 if b & 0x80 else b


def _s16(lo: int, hi: int) -> int:
    v = lo | (hi << 8)
    return v - 0x10000 if v & 0x8000 else v


_SINE_BASE, _COSINE_BASE = 0x6F90, 0x7090     # 256 signed bytes each, amplitude +-64, period 256
_HALF_EXTENT_BASE = 0x7190                     # 0x20 (x_half, y_half) sprite-hitbox pairs (id-hi & 0x1F)
_JUMP_IMPULSE_BASE = 0x79CE                     # 9 signed words: the decaying per-frame jump Yvel arc
_ATTACK_PHASE_BASE = 0x7B04                     # 4 x 5-byte attack-phase records
_SCORE_BASE = 0xA343                            # 0x11 signed words, indexed (collectible_id - 0x4A)


def sine_table() -> list[int]:
    """[0x6F90] the 256-entry signed sine table (amplitude +-64) — the row-bounce / camera-wobble generator."""
    dg = _dgroup()
    return [_s8(dg[_SINE_BASE + i]) for i in range(256)]


def cosine_table() -> list[int]:
    """[0x7090] the quarter-phase companion of :func:`sine_table`."""
    dg = _dgroup()
    return [_s8(dg[_COSINE_BASE + i]) for i in range(256)]


def jump_impulse_table() -> list[int]:
    """[0x79CE] the 9 signed per-frame Yvel impulses of the jump arc (decays -65 -> 0, then gravity takes over)."""
    dg = _dgroup()
    return [_s16(dg[_JUMP_IMPULSE_BASE + i * 2], dg[_JUMP_IMPULSE_BASE + i * 2 + 1]) for i in range(9)]


def score_table() -> list[int]:
    """[0xA343] the collectible score values, indexed by ``collectible_id - 0x4A`` (17 signed-word entries)."""
    dg = _dgroup()
    return [_s16(dg[_SCORE_BASE + i * 2], dg[_SCORE_BASE + i * 2 + 1]) for i in range(0x11)]


def sprite_half_extents() -> list[tuple[int, int]]:
    """[0x7190] the 32 ``(x_half, y_half)`` sprite-hitbox extents, indexed by ``(sprite_id >> 8) & 0x1F``."""
    dg = _dgroup()
    return [(dg[_HALF_EXTENT_BASE + i * 2], dg[_HALF_EXTENT_BASE + i * 2 + 1]) for i in range(0x20)]


@dataclass(frozen=True)
class AttackPhase:
    """One club-attack phase [0x7B04, 5 bytes]: which anim-frame table to play, its sfx, the projectile
    damage/tolerance seed, and the phase flag (the final phase carries flag 3)."""
    frame_table_ptr: int
    sfx: int
    v19: int
    flag: int


def attack_phase_table() -> list[AttackPhase]:
    """[0x7B04] the 4 club-attack phases the FSM steps through."""
    dg = _dgroup()
    return [AttackPhase(dg[_ATTACK_PHASE_BASE + k * 5] | (dg[_ATTACK_PHASE_BASE + k * 5 + 1] << 8),
                        dg[_ATTACK_PHASE_BASE + k * 5 + 2], dg[_ATTACK_PHASE_BASE + k * 5 + 3],
                        dg[_ATTACK_PHASE_BASE + k * 5 + 4]) for k in range(4)]


# --- the region legend: what each part of the 64 KB blob IS --------------------------------------------------

@dataclass(frozen=True)
class BootRegion:
    name: str
    lo: int
    hi: int          # inclusive
    kind: str        # "code" | "table" | "text" | "graphics" | "state"
    note: str


BOOT_REGIONS = (
    BootRegion("boot_memcpy_stub", 0x0000, 0x000B, "code", "12-byte rep-movsb relocation stub"),
    BootRegion("resource_manifest", 0x000C, 0x0AC8, "text", "NUL-terminated asset filenames + DOS error strings"),
    BootRegion("scancode_char_table", 0x2301, 0x2354, "table", "keyboard make-code -> ASCII char"),
    BootRegion("initial_globals", 0x2885, 0x2DB0, "state", "boot values of the render/camera/level globals"),
    BootRegion("lookup_table_block", 0x6F60, 0x7CF0, "table",
               "sine/cosine, sprite half-extents, jump-impulse, attack-phase, anim-id + anim-seq tables"),
    BootRegion("score_table", 0xA343, 0xA37C, "table", "per-collectible score values"),
    BootRegion("camera_script_bytecode", 0xA427, 0xAB39, "table", "scripted-camera command bytecode + cmd table"),
    BootRegion("password_char_table", 0xB068, 0xB0E7, "table", "password scancode/char + validation tables"),
    BootRegion("menu_palette", 0xB118, 0xB147, "table", "the difficulty/menu 16-colour palette"),
    BootRegion("carte_marker_table", 0xB148, 0xB195, "table", "world-map level-marker positions"),
    BootRegion("boot_graphics", 0xB1C0, 0xEB4D, "graphics",
               "boot-embedded bitmaps: font glyphs, UI/sprite bitmaps, 0xFF masks (pre-asset-load)"),
)


def region_bytes(name: str) -> bytes:
    dg = _dgroup()
    for r in BOOT_REGIONS:
        if r.name == name:
            return dg[r.lo:r.hi + 1]
    raise KeyError(name)


# --- lossless proof: the decoded tables re-encode to the boot image exactly -----------------------------------

def verify_boot_manifest() -> None:
    """Prove the decoded tables are a faithful (lossless) reading of the boot image, and the region legend is
    consistent (ordered, non-overlapping, within DGROUP). Raises on any mismatch."""
    dg = _dgroup()

    # resource records re-encode to their exact bytes
    for rec in resource_table():
        got = dg[rec.offset:rec.offset + len(rec.text)]
        if got.decode("latin1") != rec.text or dg[rec.offset + len(rec.text)] != 0:
            raise AssertionError(f"resource record @0x{rec.offset:04X} does not re-encode")

    # scancode table re-encodes
    tbl = scancode_char_table()
    if dg[0x2301:0x2301 + len(tbl)].decode("latin1") != tbl:
        raise AssertionError("scancode table does not re-encode")

    # every typed numeric table re-encodes to its exact boot bytes
    def enc_s8(vals):
        return bytes(v & 0xFF for v in vals)

    def enc_s16(vals):
        out = bytearray()
        for v in vals:
            v &= 0xFFFF
            out += bytes((v & 0xFF, (v >> 8) & 0xFF))
        return bytes(out)

    checks = [
        (_SINE_BASE, enc_s8(sine_table())),
        (_COSINE_BASE, enc_s8(cosine_table())),
        (_JUMP_IMPULSE_BASE, enc_s16(jump_impulse_table())),
        (_SCORE_BASE, enc_s16(score_table())),
        (_HALF_EXTENT_BASE, bytes(b for pair in sprite_half_extents() for b in pair)),
        (_ATTACK_PHASE_BASE, b"".join(
            bytes((p.frame_table_ptr & 0xFF, (p.frame_table_ptr >> 8) & 0xFF, p.sfx, p.v19, p.flag))
            for p in attack_phase_table())),
    ]
    for base, enc in checks:
        if bytes(dg[base:base + len(enc)]) != enc:
            raise AssertionError(f"typed table @0x{base:04X} does not re-encode to the boot image")

    # region legend is ordered, non-overlapping, in range
    prev = -1
    for r in BOOT_REGIONS:
        if not (0 <= r.lo <= r.hi < 0x10000):
            raise AssertionError(f"region {r.name} out of range")
        if r.lo <= prev:
            raise AssertionError(f"region {r.name} overlaps/precedes the previous")
        prev = r.hi


if __name__ == "__main__":
    verify_boot_manifest()
    files = [r for r in resource_table() if r.is_asset]
    print(f"boot manifest OK — {len(files)} asset files, "
          f"{len(scancode_char_table())}-entry scancode table, {len(BOOT_REGIONS)} named regions")
    print(f"  decoded tables: sine/cosine (256), jump_impulse={jump_impulse_table()}, "
          f"score({len(score_table())}), half_extents({len(sprite_half_extents())}), "
          f"attack_phases={len(attack_phase_table())}")
