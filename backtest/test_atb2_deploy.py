"""Standalone (DB-free) guard for the ATB2-LONG live deploy rewire (T-2026-KYT-9050-037).

ATB2-LONG was promoted SHADOW→LIVE per operator decision (Michi, bot_results.xlsx #3).
Bot 14 previously had NO live/Cornix path for ATB2 — `_emit_atb2_shadow` called
`post_shadow_ai_signal` and guarded `if not is_shadow("ATB2", dir): return`, so a
LIVE-flipped leg would have emitted NOTHING. This guard pins the rewire to the gated
router (`post_ai_signal_gated`, pattern of bot 12 `_emit_ats2` / bot 10 `_emit_epd3`)
plus the CRITICAL has_open guard (the gated LIVE branch does no has_open/cooldown check,
and the ATB2 breakout candle stays newest for ~1h → double-post without it, Regel 4).

The static check is the load-bearing net: a runtime assertion would be swallowed by the
fleet-wide broad except blocks.

Run: py -3.13 backtest/test_atb2_deploy.py   (oder pytest backtest/test_atb2_deploy.py)
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "14_ai_atb_bot.py").read_text(encoding="utf-8")


def test_bot14_imports_gated_router_not_shadow_only():
    assert "post_ai_signal_gated" in SRC and "has_open_ai_signal" in SRC, (
        "bot 14 must import the gated router + has_open guard for the ATB2 live path"
    )
    assert "post_shadow_ai_signal" not in SRC, (
        "the shadow-only emitter must be gone — ATB2 routes through post_ai_signal_gated now"
    )


def test_emit_atb2_routes_through_the_gated_router():
    assert "def _emit_atb2(" in SRC, "the ATB2 emitter must be _emit_atb2 (routes live+shadow)"
    # The shadow-only skip that dropped a LIVE leg must be gone.
    assert 'if not shadow_gate.is_shadow("ATB2"' not in SRC, (
        "the `if not is_shadow(...)` guard would SKIP a LIVE-flipped ATB2 leg — it must be "
        "the LIVE-and-SHADOW gate instead"
    )
    assert re.search(
        r'shadow_gate\.leg_status\("ATB2",\s*direction\)\s*not in \(\s*shadow_gate\.LIVE,\s*shadow_gate\.SHADOW',
        SRC,
    ), "the ATB2 gate must let LIVE **and** SHADOW through (SILENT/RETIRED skip)"
    assert re.search(r'post_ai_signal_gated\(\s*conn,\s*"ATB2",\s*direction,\s*TARGET_CHANNEL_ID', SRC), (
        "ATB2 must emit via post_ai_signal_gated to the Cornix channel (TARGET_CHANNEL_ID)"
    )


def test_atb2_live_post_has_the_has_open_guard():
    """The gated LIVE branch (post_ai_signal) does no has_open/cooldown check; the ATB2
    breakout candle stays the newest closed candle for ~1h, so without an explicit
    has_open guard every scan re-posts a live duplicate (Regel-4 double-trade)."""
    assert re.search(r'if has_open_ai_signal\(conn,\s*symbol,\s*direction,\s*"ATB2"\):\s*\n\s*return', SRC), (
        "the has_open guard before the ATB2 gated post is missing — LIVE would double-post"
    )


def test_caller_uses_the_renamed_emitter():
    assert "_emit_atb2_shadow" not in SRC, "stale _emit_atb2_shadow reference remains"
    assert re.search(r"_emit_atb2\(conn,\s*symbol,\s*now\)", SRC), "the scan loop must call _emit_atb2"


if __name__ == "__main__":
    test_bot14_imports_gated_router_not_shadow_only()
    test_emit_atb2_routes_through_the_gated_router()
    test_atb2_live_post_has_the_has_open_guard()
    test_caller_uses_the_renamed_emitter()
    print("OK — ATB2-LONG live deploy rewire contract holds")
