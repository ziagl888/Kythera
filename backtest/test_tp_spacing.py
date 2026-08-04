# backtest/test_tp_spacing.py — T-2026-KYT-9050-098: minimum gap between published TPs.
#
# Background: `hvn_sr_trade_geometry` filters its target candidates against the ENTRY only
# (`x > entry1 * 1.01`). The first hop is therefore guaranteed 1 %, every hop after it is
# whatever the raw S/R level list happens to give, and nothing thins it. Measured on
# closed_ai_signals since the P2.31 fix (2026-08-04): on the legs using that path, 54 %
# (EPD3) to 69 % (MAX1) of neighbouring published gaps sit under 1 %, and in 23-34 % of
# signals the WHOLE TP1..TP3 ladder spans less than 1 % — three Cornix tranches inside one
# tick of movement, hit together in 16-24 % of trades. The legs going through
# `calculate_smart_targets` (which thins by 1 x ATR) show 2-7 % instead.
#
# `thin_targets` adds the missing TP-to-TP floor. The two load-bearing properties:
#
#   1. It only ever thins when the candidate pool is DEEPER than what gets published —
#      otherwise a target would be dropped without replacement. That is the operator's
#      own condition and it is what keeps the `n_show=len(targets)` emitters (bot 7/18/
#      24/25 and bot 10's EPD2 legacy path) out of it.
#   2. The gap is measured against the last KEPT target, not the previous candidate.
#      Measured pairwise, a run of near-identical levels passes every individual check
#      and still clusters.
#
# Runs without a DB:  python backtest/test_tp_spacing.py

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.trade_utils import (  # noqa: E402
    MIN_TP_GAP_PCT,
    N_PUBLISHED_TARGETS,
    ensure_min_tp_distance,
    thin_targets,
)

#: The legs whose published ladder is a slice of a deeper pool — these are thinned.
THINNED_BOTS = [
    "9_ai_sr_bot.py",
    "12_ai_ats_bot.py",
    "13_ai_rub_bot.py",
    "14_ai_atb_bot.py",
    "34_ai_max1_bot.py",
    "36_ai_lis1_bot.py",
    "37_ai_tsm1_bot.py",
    "38_ai_skw1_bot.py",
    "39_ai_xsm1_bot.py",
]


def _src(name: str) -> str:
    with open(os.path.join(ROOT, name), encoding="utf-8") as fh:
        return fh.read()


# ─────────────────────────────────────────────────────────────────────────────
# thin_targets semantics
# ─────────────────────────────────────────────────────────────────────────────


def test_drops_the_target_that_sits_too_close():
    """A 0.3 % follower is skipped in favour of the next separated level."""
    # entry 100 → candidates at +1.2 %, +1.5 % (too close), +2.6 %, +4.0 %
    out = thin_targets([101.2, 101.5, 102.6, 104.0], 100.0, True, keep=3)
    assert out == [101.2, 102.6, 104.0], out


def test_gap_is_measured_against_the_last_kept_not_the_previous_candidate():
    """The clustering case: each step is +0.6 %, so pairwise every hop 'passes'.

    Measured against the previous CANDIDATE this list survives untouched and the
    published ladder still spans 1.8 % across three TPs. Against the last KEPT
    target only every other level survives — which is the point.
    """
    cands = [100.6, 101.2, 101.8, 102.4, 103.0, 103.6]
    out = thin_targets(cands, 100.0, True, keep=3)
    assert out == [100.6, 101.8, 103.0], out
    gaps = [(out[i + 1] - out[i]) / 100.0 * 100.0 for i in range(len(out) - 1)]
    assert all(g >= MIN_TP_GAP_PCT for g in gaps), gaps


def test_short_side_measures_distance_downwards():
    """For a SHORT the ladder runs below entry; the same floor applies."""
    out = thin_targets([98.8, 98.5, 97.4, 96.0], 100.0, False, keep=3)
    assert out == [98.8, 97.4, 96.0], out


def test_never_thins_when_the_pool_is_not_deeper_than_the_ladder():
    """The operator's condition: only skip when not everything gets published.

    Bot 10's EPD2 legacy path and bots 7/18/24/25 publish `len(targets)` — for them
    the pool IS the ladder, and thinning would delete a target with nothing to put
    in its place.
    """
    tight = [100.2, 100.4, 100.6]
    assert thin_targets(tight, 100.0, True, keep=3) == tight
    assert thin_targets(tight, 100.0, True, keep=5) == tight
    # One more candidate than published → thinning is allowed again.
    assert thin_targets(tight + [102.0], 100.0, True, keep=3) == [100.2, 102.0]


def test_returns_at_most_keep():
    wide = [101.0, 103.0, 105.0, 107.0, 109.0]
    assert len(thin_targets(wide, 100.0, True, keep=3)) == 3


def test_sparse_pool_returns_fewer_and_the_5pct_backstop_fills_in():
    """A pool with no second separated level yields a short ladder — on purpose.

    `ensure_min_tp_distance` runs afterwards and appends its 5 % target, so the
    published ladder does not silently shrink to one TP. That ordering is the
    contract; reversing it would let the backstop be thinned away again.
    """
    cands = [100.2, 100.3, 100.4, 100.5]
    thinned = thin_targets(cands, 100.0, True, keep=3)
    assert thinned == [100.2], thinned
    final = ensure_min_tp_distance(thinned, 100.0, True, min_pct=0.05)
    assert final == [100.2, 105.0], final


def test_degenerate_input_is_passed_through():
    assert thin_targets([], 100.0, True, keep=3) == []
    assert thin_targets([101.0], 100.0, True, keep=3) == [101.0]
    # keep=0 and a non-positive entry must not silently reorder anything.
    assert thin_targets([101.0, 101.1], 100.0, True, keep=0) == [101.0, 101.1]
    assert thin_targets([101.0, 101.1], 0.0, True, keep=1) == [101.0, 101.1]


def test_disabling_the_gap_is_a_passthrough():
    cands = [100.1, 100.2, 100.3, 100.4]
    assert thin_targets(cands, 100.0, True, keep=2, min_gap_pct=0.0) == cands


def test_epd3_shaped_ladder_from_the_live_measurement():
    """The measured EPD3 median: TP1 1.79 %, whole TP1..TP3 span 2.09 %.

    The tight variant (all three inside 1 %) is the 23 % case this exists for —
    after thinning the ladder reaches into the pool instead of stacking.
    """
    entry = 100.0
    pool = [101.79, 101.95, 102.10, 103.40, 104.90, 106.20]
    out = thin_targets(pool, entry, True, keep=N_PUBLISHED_TARGETS)
    assert out == [101.79, 103.40, 104.90], out
    span = (out[-1] - out[0]) / entry * 100.0
    assert span > 2.09, span  # wider than today's median ladder


# ─────────────────────────────────────────────────────────────────────────────
# wiring (source guards — loading these bots needs the DB + heavy deps)
# ─────────────────────────────────────────────────────────────────────────────

_CALL = re.compile(r"ensure_min_tp_distance\(\s*\n\s*thin_targets\(t_cands\[:20\],[^)]*keep=N_PUBLISHED_TARGETS\)")


def test_every_thinned_bot_routes_its_candidates_through_thin_targets():
    """Fails if a call site is added or reverted to the raw `t_cands[:20]`."""
    for name in THINNED_BOTS:
        src = _src(name)
        assert _CALL.search(src), f"{name}: no thin_targets(...) before ensure_min_tp_distance"
        # No raw slice left on the thinned path — that would be an unthinned ladder.
        assert "ensure_min_tp_distance(t_cands[:20]" not in src, f"{name}: raw candidate slice still published"


def test_thinned_bots_import_the_shared_constant_not_a_literal():
    """A per-bot literal is how the entry-side 1 % ended up on one hop only."""
    for name in THINNED_BOTS:
        src = _src(name)
        assert "N_PUBLISHED_TARGETS" in src, name
        assert re.search(r"keep\s*=\s*\d", src) is None, f"{name}: hardcoded keep"


def test_epd2_legacy_path_is_deliberately_not_thinned():
    """Bot 10 has BOTH paths — EPD3 (3 of up to 20) and the EPD2 legacy leg that
    publishes its full list. Only the first may be thinned."""
    src = _src("10_pump_dump_detector.py")
    assert src.count("thin_targets(t_cands") == 1, "expected exactly one thinned path in bot 10"
    assert "ensure_min_tp_distance(t_cands[:20]" in src, "the EPD2 legacy path must keep its raw pool"
    assert "n_show=len(targets)" in src


def test_smart_targets_legs_are_untouched():
    """`calculate_smart_targets` already thins by 1 x ATR — it must not be double-thinned."""
    src = _src(os.path.join("core", "trade_utils.py"))
    body = src[src.index("def calculate_smart_targets") :]
    body = body[: body.index("\ndef ", 1)]
    assert "thin_targets" not in body
    assert "min_tp_spacing" in body


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
    print(f"\n{failures} failure(s)")
    sys.exit(1 if failures else 0)
