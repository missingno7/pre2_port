"""The native VGA surface — the display-adapter state the VM-less game needs, with no dos_re.

The recovered game touches exactly three pieces of "VGA hardware": the 256-entry DAC palette (read by the
renderer, programmed through the 3C7/3C8/3C9 port protocol by the recovered fade/palette code via
``pre2.views.palette.write_dac``), the 6→8-bit DAC component expansion, and the EGA plane-shadow layout
inside the 1.25 MB native memory image. This module owns all three so the standalone runtime has zero
emulator dependency; the WORKBENCH's ``dos_re.dos.DOSMachine`` exposes the identical surface (attribute
names + port semantics), so every bridge/native module works over either.
"""
from __future__ import annotations

# The EGA plane shadow: 4 x 64 KB planes stored above the 1 MB real-mode space in the native memory image
# (the same layout dos_re.memory uses, asserted equal by tests/test_native.py so the two can never drift).
EGA_APERTURE = 0x100000
EGA_PLANE_STRIDE = 0x10000


def _dac8(v6: int) -> int:
    """Expand a 6-bit VGA DAC component to 8 bits the way real VGA does: ``v<<2`` alone maxes at 252
    (slightly dark); replicating the high bits (``| v>>4``) makes 63 -> 255, matching hardware."""
    v6 &= 0x3F
    return (v6 << 2) | (v6 >> 4)


# the power-on DAC: first 16 entries = the EGA palette, the rest a deterministic grayscale ramp until the
# game programs the real colours through 3C8/3C9 (same default the workbench DOSMachine uses).
_EGA16 = (
    (0x00, 0x00, 0x00), (0x00, 0x00, 0xAA), (0x00, 0xAA, 0x00), (0x00, 0xAA, 0xAA),
    (0xAA, 0x00, 0x00), (0xAA, 0x00, 0xAA), (0xAA, 0x55, 0x00), (0xAA, 0xAA, 0xAA),
    (0x55, 0x55, 0x55), (0x55, 0x55, 0xFF), (0x55, 0xFF, 0x55), (0x55, 0xFF, 0xFF),
    (0xFF, 0x55, 0x55), (0xFF, 0x55, 0xFF), (0xFF, 0xFF, 0x55), (0xFF, 0xFF, 0xFF),
)


class NativeVGA:
    """The stand-alone display adapter: ``vga_palette`` + the DAC port protocol.

    A drop-in for the ``dos`` parameter every native/bridge call takes (``dos.vga_palette`` reads +
    ``dos._track_vga_dac_ports`` writes are the entire surface the VM-less game uses)."""

    def __init__(self):
        self.vga_palette: list[tuple[int, int, int]] = list(_EGA16) + [(i, i, i) for i in range(16, 256)]
        # The BIOS video mode the scene classifier reads (bridge/scene_state derive_scene_kind). The native
        # game runs its gameplay + 0Dh scenes in planar mode 0x0D; the 13h title screens are rendered by the
        # runner directly (MODE_LINEAR FrontEndScene) and never pass through the classifier — so the native
        # adapter stays in 0x0D permanently. (The workbench DOSMachine boots at text mode 3 and tracks the
        # game's int10 calls instead; caught live by the gap-snapshot facility on a level-15 render path.)
        self.video_mode = 0x0D
        self._dac_write_index = 0
        self._dac_read_index = 0
        self._dac_component = 0
        self._dac_latch: list[int] = []

    def _track_vga_dac_ports(self, port: int, value: int, bits: int) -> None:
        """The 3C7/3C8/3C9 DAC write protocol, semantics identical to the workbench DOSMachine's."""
        if bits == 16:
            self._track_vga_dac_ports(port, value & 0xFF, 8)
            self._track_vga_dac_ports((port + 1) & 0xFFFF, (value >> 8) & 0xFF, 8)
            return
        if bits != 8:
            return
        value &= 0xFF
        if port == 0x03C8:
            self._dac_write_index = value
            self._dac_component = 0
            self._dac_latch = []
            return
        if port == 0x03C7:
            self._dac_read_index = value
            self._dac_component = 0
            return
        if port != 0x03C9:
            return
        self._dac_latch.append(_dac8(value))
        self._dac_component += 1
        if self._dac_component >= 3:
            r, g, b = (self._dac_latch + [0, 0, 0])[:3]
            idx = self._dac_write_index & 0xFF
            self.vga_palette[idx] = (r, g, b)
            self._dac_write_index = (idx + 1) & 0xFF
            self._dac_component = 0
            self._dac_latch = []
