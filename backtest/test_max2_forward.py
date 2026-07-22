"""Standalone (DB-free) guard for the MAX2 fork + the Main-Channel retirement.

Background (T-2026-KYT-9050-020): the classic "Main Channel" detector
(strat_main_channel.py, grade C-/-77 PnL, a near-duplicate of Support Resistance
gated to config.MAIN_CHANNEL_COINS) is retired and replaced by MAX2. MAX2 is NOT
a model and NOT a process: it is an inline fork of the SRA2-LONG emission in
9_ai_sr_bot.py (_emit_max2). Whenever SRA2 LONG fires (prob>=threshold) for a coin
in MAIN_CHANNEL_COINS, the SAME trade (same prob + entry/SL/target geometry) is
additionally posted under tag "MAX2" to CH_MAIN. The only filter is the coin
whitelist, exactly like the retired bot (operator decision Michi).

What this guard protects (static regex is the load-bearing net — a runtime assert
would be swallowed by the fleet-wide broad except blocks, lesson T-2026-CU-9050-024):

  * MAX2 reuses the SRA2 trade: the fork fires AFTER the geometry is computed, only
    for LONG, only for whitelisted coins, and routes to CH_MAIN under tag MAX2.
  * MAX2 owns its dedup namespace (has_open("MAX2")) — rule 6, so it never blocks
    or is blocked by the SRA2 active-trade check.
  * The classic Main-Channel dispatch is gone from 3_detectors.py (no analyze_main,
    no timed_scan('Main Channel'), no MAIN_CHANNEL_COINS import there).
  * MAX2 LONG is default-LIVE in the gate (leg_status), the deliberate operator
    posture — collision-free because CH_AI_SR is not Cornix-executed.

Run: python backtest/test_max2_forward.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import shadow_gate  # noqa: E402

SRC = (ROOT / "9_ai_sr_bot.py").read_text(encoding="utf-8")
DET = (ROOT / "3_detectors.py").read_text(encoding="utf-8")

# The _emit_max2 body, sliced out so substring probes cannot leak into other funcs.
_MAX2 = SRC[SRC.index("def _emit_max2(") : SRC.index("def _emit_sra2_shadow(")]
# The _emit_sra2_shadow body, where the fork is wired.
_SRA2 = SRC[SRC.index("def _emit_sra2_shadow(") :]


# ------------------------------------------------------------ MAX2 fork wiring


def test_emit_max2_exists_with_the_reused_geometry_signature():
    """MAX2 takes the ALREADY-computed SRA2 geometry (prob/entry1/entry2/sl/targets)
    — it must not recompute, or MAX2 would drift from the SRA2 trade it mirrors."""
    assert "def _emit_max2(conn, coin, prob, entry1, entry2, sl, targets) -> None:" in SRC, (
        "the MAX2 fork helper is gone or its reuse-the-SRA2-geometry signature changed"
    )


def test_max2_posts_to_the_main_channel_under_its_own_tag():
    assert "post_ai_signal_gated(" in _MAX2, "MAX2 no longer posts through the gated router"
    assert '"MAX2"' in _MAX2 and '"LONG"' in _MAX2, "MAX2 no longer posts under tag MAX2 / LONG"
    assert "_kcfg.CH_MAIN" in _MAX2, "MAX2 no longer routes to the Main channel (CH_MAIN)"


def test_max2_has_its_own_dedup_namespace():
    """Rule 6: a distinct tag means a distinct cooldown/position namespace. MAX2
    must probe has_open on "MAX2", never on SRA2 — otherwise the two legs would
    suppress each other on the same coin."""
    assert 'has_open_ai_signal(conn, coin, "LONG", "MAX2")' in _MAX2, (
        "MAX2 lost its own active-trade guard (has_open on tag MAX2)"
    )


def test_max2_is_gate_guarded_before_it_posts():
    """MAX2 must respect the master shadow switch + its leg status, exactly like
    the SRA2 emission — so a rollback to shadow (or KYTHERA_SHADOW_POSTING=0)
    actually silences it."""
    assert 'shadow_gate.leg_status("MAX2", "LONG")' in _MAX2, "MAX2 no longer checks its own leg status"
    assert "shadow_gate.shadow_posting_enabled()" in _MAX2, "MAX2 ignores the master shadow kill-switch"


def test_fork_fires_only_for_long_whitelisted_coins():
    assert 'if direction == "LONG" and coin in _kcfg.MAIN_CHANNEL_COINS:' in _SRA2, (
        "the MAX2 fork guard changed — it must fire only for LONG and only for MAIN_CHANNEL_COINS"
    )
    assert "_emit_max2(conn, coin, prob, entry1, entry2, sl, targets)" in _SRA2, (
        "the MAX2 fork call is gone or no longer passes the reused SRA2 geometry"
    )


def test_fork_fires_after_the_geometry_is_built():
    """The fork must sit AFTER get_hvn_and_sr_levels / target construction so it
    hands MAX2 the SAME sl/targets the SRA2 post used — not stale/unset values."""
    geom = _SRA2.index("supps, resis = get_hvn_and_sr_levels")
    fork = _SRA2.index("_emit_max2(conn, coin, prob")
    assert geom < fork, "the MAX2 fork moved above the geometry computation — it would reuse unbuilt values"


# ------------------------------------------------------- classic dispatch retired


def test_classic_main_channel_dispatch_is_gone():
    assert "analyze_main" not in DET, "the retired Main-Channel strategy is still imported/dispatched in 3_detectors"
    assert "timed_scan('Main Channel'" not in DET, "3_detectors still dispatches the classic 'Main Channel' scan"


def test_main_channel_coins_no_longer_pulled_into_the_detector():
    """The whitelist now lives solely on the MAX2 side (bot 9). Leaving the import
    in 3_detectors would be dead (ruff F401) and imply a dispatch that no longer exists."""
    assert "MAIN_CHANNEL_COINS" not in DET, "3_detectors still references MAIN_CHANNEL_COINS after the retirement"


def test_one_h_roster_drops_main_channel():
    assert "return ('5 Percent', 'Support Resistance')" in DET, "the 1h strategy roster changed unexpectedly"
    assert "'Main Channel')" not in DET, "the 1h roster still advertises 'Main Channel'"


# ------------------------------------------------------------- gate posture (live)


def test_max2_long_is_default_live():
    """Operator posture: MAX2 LONG posts live Cornix to CH_MAIN (collision-free
    because CH_AI_SR is not Cornix-executed). Default LIVE = NOT listed in the
    _LIFECYCLE register."""
    assert shadow_gate.leg_status("MAX2", "LONG") == shadow_gate.LIVE, (
        "MAX2 LONG is no longer default-LIVE — a stray _LIFECYCLE entry pulled it back to shadow/silent"
    )
    assert shadow_gate.is_live("MAX2", "LONG"), "shadow_gate.is_live disagrees with the intended MAX2 posture"


def test_max2_is_not_flagged_retired():
    assert not shadow_gate.is_retired("MAX2"), "MAX2 must not collide with a retired-tag prefix"


if __name__ == "__main__":
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            _fn()
    print("OK — MAX2 fork + Main-Channel retirement holds")
