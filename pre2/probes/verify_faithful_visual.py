"""Phase-A check: the faithful visual dispatcher routes each scene category to the right leaf.

Loads one labeled snapshot per visual mode and reports derive_scene_kind(); for GAMEPLAY/IRIS it
also runs render_visual_planes (the recovered dispatch) and confirms a frame is produced. The iris
PIXEL fidelity (render_frame base + recovered iris clear vs ASM) is proven separately at a clean
instant in verify_live_faithful-style driving (diff = the known moving-sprite phase residual, not an
iris error). This probe is the routing + no-crash check.
"""
import sys; sys.path.insert(0, '.')

from pre2.runtime import load_pre2_snapshot
from pre2.bridge.scene_state import derive_scene_kind
from pre2.bridge.live_render import render_visual_planes
from pre2.recovered.faithful_visual import SceneKind, FaithfulVisualGap

_SNAPS = [
    ('gameplay', 'snapshot_pre2_gameplay_20260621_185902', SceneKind.GAMEPLAY),
    ('iris',     'snapshot_pre2_tally_iris_20260622_002633', SceneKind.IRIS),
    ('intro',    'snapshot_pre2_intro_image_20260622_163804', SceneKind.IMAGE),
    ('title',    'snapshot_pre2_title_image_20260622_163923', SceneKind.IMAGE),
    ('menu',     'snapshot_pre2_modeselect_20260623_075918', SceneKind.SCENE),
    ('map',      'snapshot_pre2_mapscroll_20260623_110253', SceneKind.SCENE),
    ('tally',    'snapshot_pre2_tally_clearspan_20260621_173821', SceneKind.IRIS),  # clearspan = iris running
]


def main():
    ok = True
    for label, snap, expect in _SNAPS:
        rt = load_pre2_snapshot('assets/pre2.exe', 'artifacts/' + snap, game_root='assets',
                                native_replacements=True)
        m, dos = rt.cpu.mem, rt.dos
        kind = derive_scene_kind(m, dos)
        composed = ''
        try:
            planes, page, k2 = render_visual_planes(m, dos, game_root='assets')
            composed = f' -> composed page={page:#06x}'
            if kind not in (SceneKind.GAMEPLAY, SceneKind.IRIS):
                composed = ' -> rendered but expected a GAP!'; ok = False
        except FaithfulVisualGap as gap:
            composed = ' -> GAP (no fallback): ' + str(gap).split('— ')[-1][:60]
            if kind in (SceneKind.GAMEPLAY, SceneKind.IRIS):
                composed = ' -> unexpected GAP!'; ok = False
        note = ''
        if expect is not None and kind != expect:
            note = f'  (EXPECTED {expect.name})'; ok = False
        print(f'  {label:9s} video={dos.video_mode & 0x7F:#04x} -> {kind.name}{composed}{note}')
    print('FAITHFUL VISUAL DISPATCH ROUTING:', 'PASS' if ok else 'CHECK')
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
