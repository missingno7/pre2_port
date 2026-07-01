"""The shared SPRITE BANK builder (1030:2DFA) — turn the decoded SPRITES.SQZ into the in-memory sprite bank.

At front-end time (main 0x126 -> 2dfa, while the title is still on screen) the game decodes ``SPRITES.SQZ``
(the recovered LZW ``unpack_sqz``) into a flat run of **4-plane** sprites, then rebuilds it into the
**5-plane** bank the renderer uses: each sprite gains a leading OR-MASK plane (the union of its 4 colour
planes — the silhouette used for transparency/collision). The bank grows exactly 5/4 = 1.25x, which is the
``count + count/4`` paragraph expansion the ASM reserves at 1030:2E2C before the in-place demux at 1030:2EBA.

ASM model (capstone on the unpacked segment), per sprite, source si / dest di, plane stride ``bp``:
  * ``bp`` (per-plane byte size) comes from the descriptor table at DGROUP ``0x7190``: each entry is a word
    ``(al, ah)``; ``a = ror(al, 3)``; if ``a & 0xE0`` then ``a = (a & 0x1F) + 1``; ``bp = a * ah`` [asm 2E95-2EAD].
    The table ends at the first entry whose low byte is 0 [asm 2E97 ``or al,al; je``].
  * the demux [asm 2EBA-2ECF] writes ``bp`` mask bytes ``al = src[j] | src[bp+j] | src[2bp+j] | src[3bp+j]``
    (OR of the 4 planes), then [asm 2ED1 ``rep movsw``] copies the 4 source planes verbatim -> ``mask + 4 planes``.

The bank is allocated to a paragraph boundary, so the very end carries a few bytes of uninitialised slack past
the last sprite (26 bytes in the shipped data); that slack is NOT sprite data and is not reproduced here (the
recovered output is the exact ``sum(5*bp)`` sprite bytes). This is the shared bank ``native_level_load`` defers
([[pre2-level-init-island]]) — recovering it here unblocks cold-boot-from-files (#10).
"""
from __future__ import annotations

from pre2.islands import oracle_link


def sprite_descriptor_sizes(table: bytes) -> list[int]:
    """[asm 2E95-2EAD] Parse the ``0x7190`` descriptor table into the per-sprite per-plane byte size ``bp``.

    Each entry is a little-endian word ``(al, ah)``; the count nibble is ``ror(al, 3)`` with a high-bit
    fixup, and ``bp = count * ah``. The table ends at the first entry with ``al == 0``."""
    sizes: list[int] = []
    i = 0
    while i + 1 < len(table):
        al, ah = table[i], table[i + 1]
        if al == 0:                                   # [asm 2E97] or al,al; je -> end of table
            break
        a = ((al >> 3) | (al << 5)) & 0xFF            # [asm 2E9B] ror al, 3
        if a & 0xE0:                                  # [asm 2EA1] test al,0xE0; jne
            a = (a & 0x1F) + 1                        # [asm 2EA5-2EA7] and 0x1F; inc
        sizes.append(a * ah)                          # [asm 2EAB] mul ah
        i += 2
    return sizes


@oracle_link("1030:2DFA",
             "shared sprite-bank build: decode SPRITES.SQZ (unpack_sqz) then prepend an OR-mask plane to "
             "each 4-plane sprite (-> 5 planes) per the 0x7190 descriptor table; the 1.25x demux at 2EBA.",
             "VERIFIED", merge_target="native_level_load")
def build_sprite_bank(sprites: bytes, table: bytes) -> bytes:
    """Build the 5-plane shared sprite bank from the decoded ``SPRITES.SQZ`` and the ``0x7190`` table.

    ``sprites`` is the flat 4-plane data (``unpack_sqz(SPRITES.SQZ)``); the result is each sprite as
    ``mask + plane0..3`` concatenated (``sum(5*bp)`` bytes), byte-exact vs the ASM's in-memory bank."""
    out = bytearray()
    off = 0
    for bp in sprite_descriptor_sizes(table):
        pl = sprites[off:off + 4 * bp]                # [asm 2EAF-2EB7] the sprite's 4 planes
        if len(pl) < 4 * bp:
            break
        mask = bytes(pl[j] | pl[bp + j] | pl[2 * bp + j] | pl[3 * bp + j] for j in range(bp))
        out += mask                                   # [asm 2EBA-2ECF] the OR-mask plane
        out += pl                                     # [asm 2ED1] rep movsw: the 4 planes verbatim
        off += 4 * bp
    return bytes(out)


@oracle_link("1030:2EE2",
             "shared sprite-bank offset tables: per sprite the (offset, segment) far pointer into the "
             "5-plane bank, accumulating bp*5 with the offset wrapping at 0x1000 (segment += 0x100).",
             "VERIFIED", merge_target="native_level_load")
def build_sprite_offset_tables(table: bytes, base_segment: int) -> tuple[list[int], list[int]]:
    """[asm 2EE2-2F2A] The per-sprite far pointers into the 5-plane bank — the ``[0x5f48]`` offset table and
    the ``[0x62e8]`` segment table the game indexes by sprite id.

    Offsets accumulate the 5-plane size ``bp*5``; when an offset reaches ``0x1000`` the segment advances
    ``0x100`` paragraphs. The normalize happens AFTER storing (asm order), so a sprite landing on the boundary
    has a stored offset ``>= 0x1000``. ``base_segment`` is where the bank is loaded (``[0x2db4]``)."""
    offsets: list[int] = []
    segments: list[int] = []
    dx = 0
    bx = base_segment & 0xFFFF
    for bp in sprite_descriptor_sizes(table):
        offsets.append(dx)                            # [asm 2F0E] [di+0x5f48] = dx
        segments.append(bx)                           # [asm 2F12] [di+0x62e8] = bx
        if dx >= 0x1000:                              # [asm 2F16-2F22] cmp 0x1000; sub 0x1000; add bx,0x100
            dx -= 0x1000
            bx = (bx + 0x100) & 0xFFFF
        dx += bp * 5                                  # [asm 2F26] add dx, ax  (ax = bp*5)
    return offsets, segments
