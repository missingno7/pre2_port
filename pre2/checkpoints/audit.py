"""Runtime hook audit — the unambiguous picture of what is actually installed, firing, and verified.

`recovered code existing in pre2/recovered` is NOT the same as `a live hybrid replacement that fires`. This
module reports the truth at runtime: every `@registry.replace` hook (installed by `install_pre2_replacements`)
with its address, module, live category, fired count, and whether a verify oracle exists for it — plus the
recovered routines that are NOT separately hooked (helpers folded into a parent hook, shadow-only pieces, the
verify-only HUD, and the still-ASM gaps).

Use ``build_hook_audit(rt, frames=...)`` (or ``python scripts/play.py ... --hook-audit``). The maintained
`_CATEGORY` / `_NOT_SEPARATELY_HOOKED` tables are drift-checked against the live registry: a registered hook
missing from `_CATEGORY` is reported as ``UNCLASSIFIED`` so this file cannot quietly rot.
"""
from __future__ import annotations

from dataclasses import dataclass

from dos_re.hooks import registry

# live category of each registered hook. "live" = authoritative (writes its contract, skips the ASM);
# "live-passthrough" = installed but runs the ASM body in live mode (timing-critical, verify/standalone only).
_CATEGORY = {
    # --- gameplay object system ---
    "object_tick": "live",                    # 684E whole walker
    "object_velocity": "live-subsumed",       # 6861 — installed but object_tick(684E) short-circuits it in
                                              #        hybrid; fires only in verify mode (per-leaf oracle)
    "object_render": "live",                  # 26FA moving sprites
    "second_pass_project_entity": "live",     # 7F26 2nd-pass entity -> object-list projection
    "player_x_integrate": "live",             # 5A0F player-FSM leaf (horizontal kinematics, inline block)
    "player_y_integrate": "live",             # 5A36 player-FSM leaf (vertical kinematics; collision 5A96 corrects)
    "player_tick_timers": "live",             # 5A47 player-FSM leaf (8 saturating per-frame countdown timers)
    # --- gameplay frame renderer ---
    "frame_grid": "live", "frame_tile_row": "live", "frame_scroll_copy": "live",
    "frame_panel_copy": "live-passthrough",   # 3054 vsync-paced curtain reveal — ASM timing in live
    "foreground_tiles": "live", "sprite_blit": "live", "bg_anim_advance": "live",  # 367D = BACKGROUND tile anim
    "particles_draw": "live", "firefly_sim": "live", "palette_fade": "live", "camera_shake_apply": "live",
    # --- other scenes / load / audio (not the gameplay frame loop) ---
    "scroll_blit": "live", "scroll_shift": "live", "draw_string": "live", "oldies_glyph": "live",
    "gameover_scroll": "live", "tally_panel": "live", "iris_transition": "live",
    "sqz_decompress": "live", "sprite_decode_local": "live", "sprite_decode_shared": "live",
    "audio_mix_channel": "live", "audio_tracker_tick": "live",
}

# modules that install a verify-exit oracle (enable_pre2_hook_verification). A hook in one of these is
# verify-enabled (the ASM can be diffed against it).
_VERIFY_MODULES = {
    "sqz", "sprite_decode", "blit", "frame", "audio", "tracker", "object_render", "object_update",
    "object_inject", "sprite_classify", "palette", "animation", "camera_shake", "fireflies",
    "gameover_scroll", "tally_panel", "hud", "transition", "text", "present", "particles", "foreground_tiles",
    "player",
}

# Recovered routines that are NOT their own live replacement hook — the rest of the runtime truth.
# (name, origin, status, note)
_NOT_SEPARATELY_HOOKED = [
    ("apply_velocity", "1030:6861", "live-in-parent", "runs inside object_tick (also hooked at 6861 but subsumed)"),
    ("advance_animation (object)", "1030:6881", "live-in-parent", "runs inside object_tick"),
    ("despawn_check", "1030:8084", "live-in-parent", "runs inside object_tick handlers"),
    ("on_screen_tile", "1030:8022", "live-in-parent", "inside object_tick + second_pass_project_entity"),
    ("handle_object_* (idx0-12)", "cs:[0x6AA9]", "live-in-parent", "the AI handlers, dispatched inside object_tick"),
    ("terrain_collision", "1030:698C", "live-in-parent", "runs inside object_tick"),
    ("spawn_effects / find_free", "1030:7FD9/8014", "live-in-parent", "runs inside object_tick (idx2/3/4 spawn)"),
    ("rng_ror / rng_lcg", "1030:26CF/39DF", "live-in-parent", "runs inside object_tick (idx6) + 2nd-pass"),
    ("find_free_object_slot", "1030:806C", "live-in-parent", "runs inside second_pass_project_entity"),
    ("2nd-pass wrappers idx3/5-8/9/11", "7ED8/7EB5/7E97/7D6E", "ASM (calls live worker)", "4-insn stubs: call the live 7F26 + set mode; disasm'd, deliberately not accumulated as shadow code"),
    ("lookup_anim_frame", "1030:6954", "ASM (inline)", "anim-frame table lookup inline in the 2nd-pass loop; disasm'd, not hooked"),
    ("draw_hud", "1030:45B8", "verify-only", "recovered + diffed; the HUD stays ASM-drawn live (incremental, two-page)"),
    ("player FSM (rest)", "1030:~5890..5A95", "ASM / partially recovered", "X+Y integrate + timers (5A0F/5A36/5A47) live; cs:[0x7D2F] per-input handlers + collision+tile-interaction (5A96/cs:[0x7D9B]) still ASM"),
    ("player physics primitives", "1030:62B1/62EC/6333/6309", "recovered / shadow-only", "accel/friction_dir/friction_sym/gravity; pure+shadow-verified (go live via the handler/dispatch composition, not as separate hooks)"),
    ("player anim primitives", "1030:635D/6374/638B", "recovered / shadow-only", "set_anim(a/b)/advance_anim; pure+shadow-verified (write [0x4F20] frame, [0x4F28] seq-ptr; go live via the handler composition)"),
    ("player FSM state-select", "1030:5921..595C", "recovered / shadow-only", "player_select_anim_id: input bitmask -> anim_id via table [0x7B7F] + reset; shadow-verified 1997/1997"),
    ("player FSM handler: run", "1030:5EC4 (anim_id=1)", "recovered / shadow-only", "player_state_run: composition of the primitives; shadow-verified 795/795 main path ([0x6BD0] override tail not yet recovered)"),
    ("player FSM handler: anim5", "1030:5E96 (anim_id=5)", "recovered / shadow-only", "player_state_anim5: composition (set_anim/advance/friction_sym/charge_6BCE); shadow-verified 14/14"),
    ("player FSM handler: idle", "1030:5CDB (anim_id=0)", "recovered / shadow-only", "player_state_idle + player_emit_trail (5E11): airborne/moving+trail/default/long-idle/fidget paths; shadow-verified 719/719+88/88 (anim13 path 5D8A unwitnessed)"),
    ("player FSM handler: jump", "1030:5F30 (anim_id=2)", "recovered / shadow-only", "player_state_jump: jump-arc table 0x79CE / gravity + horizontal + set_anim(2) + 2x friction; falls to idle on [0x6BE0]; shadow-verified 288/288+4/4"),
    ("player FSM handler: anim8", "1030:5CCE (anim_id=8)", "recovered / shadow-only", "player_state_anim8: friction_dir/sym + set_anim (al=clobbered Xvel low) + advance; shadow-verified 134/134"),
    ("player FSM handler: anim4", "1030:5E62 (anim_id=4)", "recovered / shadow-only", "player_state_anim4: [0x6BD3]=0/[0x6BE1]=4/charge; accel(0x20)+set_anim or fall to idle; shadow-verified 11/11"),
    ("player FSM dispatch (all 6)", "1030:5A0B cs:[0x7D2F]", "recovered / shadow-only", "player_dispatch_handler: anim_id -> the 6 recovered handlers; full-dispatch shadow 1980/1980 (L1) + 211/211 (L6) byte-exact; anim_id 3/6/7 (0x5F96 attack) + idle anim13 sub-path = gaps"),
    ("player FSM handler: attack", "1030:5F96 (=override 5F93)", "recovered / shadow-only", "player_state_attack: audio (play_sfx 0x282) + projectile spawn (0x4F2E) + render-sprite; shadow-verified 100/100 (L1) + 88/88 (L6), sfx matched; 5F93 override = same body with al=[0x4F27]"),
    ("player FSM step (full)", "1030:58A7..5A0B", "recovered / shadow-only", "player_fsm_step: front-end -> select_anim_id -> dispatch (incl attack 3/6/7 + override). Only remaining gap = idle anim13 (5D8A). Full-step standalone shadow shows perturbation-class residuals on the STATEFUL attack seq -> verify-mode at the live collapse is the authority"),
    ("secondary lists 0x4F2E/0x50A8/0x5450/0x6BBE", "581E/6210/60FE/60DF", "ASM / not recovered", "other per-frame entity lists"),
]


@dataclass
class HookRow:
    name: str
    cs: int
    ip: int
    module: str
    installed: bool
    category: str
    fired: int
    verify: bool


def installed_rows() -> list[HookRow]:
    """The registered (@registry.replace) hooks, without needing a running CPU."""
    rows = []
    for (cs, ip), repl in sorted(registry.replacements.items(), key=lambda kv: kv[1].name):
        mod = getattr(repl.handler, "__module__", "?").split(".")[-1]
        cat = _CATEGORY.get(repl.name, "UNCLASSIFIED")
        rows.append(HookRow(repl.name, cs, ip, mod, True, cat, 0, mod in _VERIFY_MODULES))
    return rows


def build_hook_audit(rt, *, frames: int = 60, advance=None):
    """Install the hybrid hooks, run ``frames`` gameplay frames counting each hook's fires, return the rows.

    ``advance(rt, frame)`` steps one frame (caller supplies it so this stays runtime-agnostic)."""
    from pre2.checkpoints import install_pre2_replacements
    install_pre2_replacements(rt)
    cpu = rt.cpu
    fired: dict[str, int] = {}
    for key in list(cpu.replacement_hooks):
        fn = cpu.replacement_hooks[key]
        name = cpu.hook_names.get(key) or "%04x:%04x" % key

        def make(fn, name):
            def wrapped(c):
                fired[name] = fired.get(name, 0) + 1
                return fn(c)
            return wrapped
        cpu.replacement_hooks[key] = make(fn, name)

    if advance is not None:
        for f in range(frames):
            advance(rt, f)

    rows = installed_rows()
    for r in rows:
        r.fired = fired.get(r.name, 0)
    return rows


def format_audit(rows: list[HookRow]) -> str:
    """Render the audit table + the recovered-but-not-separately-hooked list + a drift check."""
    out = ["", "=== RUNTIME HOOK AUDIT (installed @registry.replace replacements) ===",
           f"{'hook name':<28} {'cs:ip':<11} {'module':<16} {'category':<16} {'fired':>7} {'verify':>7}"]
    order = {"live": 0, "live-subsumed": 1, "live-passthrough": 2, "UNCLASSIFIED": 9}
    for r in sorted(rows, key=lambda r: (order.get(r.category, 5), -r.fired, r.name)):
        flag = "  <-- ZERO FIRES" if (r.fired == 0 and r.category == "live") else ""
        out.append(f"{r.name:<28} {r.cs:04X}:{r.ip:04X}  {r.module:<16} {r.category:<16} "
                   f"{r.fired:>7} {'yes' if r.verify else 'no':>7}{flag}")
    # category roll-up
    from collections import Counter
    cc = Counter(r.category for r in rows)
    fired_live = sum(1 for r in rows if r.category in ("live", "live-passthrough") and r.fired)
    out.append(f"\n  installed={len(rows)}  live-and-firing={fired_live}  by-category={dict(cc)}")

    out.append("\n=== RECOVERED ROUTINES NOT SEPARATELY HOOKED (the rest of the runtime truth) ===")
    out.append(f"{'routine':<40} {'origin':<22} {'status':<22} note")
    for name, origin, status, note in _NOT_SEPARATELY_HOOKED:
        out.append(f"{name:<40} {origin:<22} {status:<22} {note}")

    unclassified = [r.name for r in rows if r.category == "UNCLASSIFIED"]
    if unclassified:
        out.append(f"\n!! DRIFT: {len(unclassified)} registered hook(s) missing from _CATEGORY: {unclassified}")
    else:
        out.append("\n  drift check: OK (every installed hook is classified).")
    return "\n".join(out)
