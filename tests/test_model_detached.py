"""Partial detachment gate: the game MODEL (pre2/game) is a clean, independent object graph.

SCOPE (important, do not over-read): this checks ONLY pre2/game. It does NOT prove the *release* is offset-free —
the released dist also runs pre2/native + pre2/recovered + pre2/views, which STILL carry ~742 offset constants and
~192k raw offset accesses (see docs/pre2/offset_free_release_plan.md). The whole-closure gate (zero offsets across
the shipped closure) is the FINISH of the offset-free-release milestone and is not done yet. This test only
guarantees the model layer stays clean while the dissolve grind removes offsets from the rest.
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GAME_DIR = ROOT / "pre2" / "game"

# packages the shipped model must NOT depend on — everything that knows the original DOS memory format or the VM.
_FORBIDDEN_PREFIXES = ("pre2.bridge", "pre2.views", "pre2.native", "pre2.recovered", "pre2.checkpoints",
                       "pre2.probes", "dos_re")


def _game_modules():
    return sorted(GAME_DIR.glob("*.py"))


def test_shipped_model_imports_nothing_from_the_detachable_machinery():
    """pre2/game imports only stdlib + pre2.game — no bridge/views/native/recovered/VM. So it detaches cleanly."""
    assert _game_modules(), "no pre2/game modules found"
    for mod in _game_modules():
        tree = ast.parse(mod.read_text(encoding="utf-8"), filename=str(mod))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                assert not name.startswith(_FORBIDDEN_PREFIXES), \
                    f"{mod.name} imports {name!r} — the shipped model must not depend on the detachable machinery"


def test_shipped_model_has_no_dgroup_memory_idioms():
    """No byte-image / offset-accessor idioms in the shipped model — it is pure named fields + references.
    Tokenised so DGROUP/offsets mentioned in docstrings or comments (prose) are ignored — only CODE counts."""
    import io
    import tokenize
    accessors = {"rb", "rw", "wb", "ww"}
    segnames = {"DATA_SEG", "DGROUP", "DGROUP_BASE"}
    for mod in _game_modules():
        toks = list(tokenize.generate_tokens(io.StringIO(mod.read_text(encoding="utf-8")).readline))
        names = [t for t in toks if t.type == tokenize.NAME]     # strings/comments are other token types -> skipped
        for t in names:
            assert t.string not in segnames, f"{mod.name}:{t.start[0]} references {t.string} in code"
        # an accessor NAME immediately followed by '(' is a raw memory read/write
        for a, b in zip(toks, toks[1:]):
            if a.type == tokenize.NAME and a.string in accessors and b.type == tokenize.OP and b.string == "(":
                raise AssertionError(f"{mod.name}:{a.start[0]} calls the raw memory accessor {a.string}(")


def test_the_model_still_round_trips_through_the_bridge():
    """Sanity: the model is detached but ATTACHABLE — the bridge can still serialise it to a byte-exact image.
    (The full corpus proof is verify_player_dataclass; this just confirms the seam is intact from this layer.)"""
    from pre2.bridge.game_layout import DataclassBackend
    from pre2.native.state import NativeGameState

    st = NativeGameState(bytearray(0x10000 + (0x1A0F << 4)))
    dcb = DataclassBackend(st, readonly_image=False)
    before = bytes(st.data)
    dcb.materialize()
    assert bytes(st.data) == before, "materialise of a freshly-seeded graph must reproduce the image"
