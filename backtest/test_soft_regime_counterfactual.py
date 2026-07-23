"""DB-free tests for tools/soft_regime_counterfactual.py (T-2026-KYT-9050-031).

Pins the pieces the report's verdict rests on:
  * reconstruct_rule — the debounce fold reproduces the effective RULE with the
    correct TREND (3-check) vs non-TREND (2-check) thresholds.
  * whipsaw — switch/dwell counting on 5-min cadence, None-safe.
  * asof_indexer — as-of lookup returns the last ts <= when (no look-ahead).
  * two_proportion_z — win-rate significance math.
  * wr_by_agreement — TP/SL bucketing by SOFT-vs-RULE agreement + shift labels.
  * annotate_soft_rule — fidelity counts recorded-RULE match, agreement uses SOFT.
  * build_soft_timeline reconstruction path consumes a raw_features-shaped frame.

Standalone (no DB, no network): run `python backtest/test_soft_regime_counterfactual.py`.
The debounce-vs-apply_debounce PARITY is already pinned by
backtest/test_regime_switch_study.py (this module folds that same _step_debounce).
"""

from __future__ import annotations

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.regime_logic import REGIME_DEBOUNCE_COUNT, TREND_DEBOUNCE_COUNT  # noqa: E402
from tools.soft_regime_counterfactual import (  # noqa: E402
    annotate_soft_rule,
    asof_indexer,
    build_timelines,
    reconstruct_rule,
    two_proportion_z,
    whipsaw,
    wr_by_agreement,
)


def _idx(n):
    return pd.date_range("2026-05-01", periods=n, freq="5min")


def test_reconstruct_rule_trend_vs_nontrend_thresholds():
    """TREND needs 3 consecutive checks, non-TREND 2 — folded from cold start."""
    assert (REGIME_DEBOUNCE_COUNT, TREND_DEBOUNCE_COUNT) == (2, 3), "thresholds moved — update expectation"
    raw = ["CHOP", "CHOP", "TREND_UP", "TREND_UP", "TREND_UP", "TREND_UP", "CHOP", "CHOP"]
    alt = ["ALT_NEUTRAL"] * len(raw)
    eff = reconstruct_rule(raw, alt, _idx(len(raw)))
    assert list(eff.values) == [
        "CHOP", "CHOP", "CHOP", "CHOP", "TREND_UP", "TREND_UP", "TREND_UP", "CHOP"
    ], list(eff.values)


def test_reconstruct_rule_resets_pending_on_break():
    """A non-consecutive candidate restarts the pending counter (no accumulation)."""
    raw = ["CHOP", "HIGH_VOLA", "CHOP", "HIGH_VOLA", "HIGH_VOLA"]  # HV never gets 2-in-a-row until the end
    alt = ["ALT_NEUTRAL"] * len(raw)
    eff = reconstruct_rule(raw, alt, _idx(len(raw)))
    # cold=CHOP; HV(1); CHOP resets→CHOP stable; HV(1); HV(2)→flip
    assert list(eff.values) == ["CHOP", "CHOP", "CHOP", "CHOP", "HIGH_VOLA"], list(eff.values)


def test_whipsaw_counts_switches_and_dwell():
    s = pd.Series(["A", "A", "A", "B", "B", "A"], index=_idx(6))
    w = whipsaw(s)
    assert w["n_switches"] == 2  # A→B, B→A
    assert w["n_episodes"] == 3
    assert w["n_checks"] == 6
    # dwell episodes: [3,2,1] checks; <1h (=12 checks) → all 3 short
    assert w["pct_episodes_under_1h"] == 100.0


def test_whipsaw_none_safe():
    s = pd.Series([None, "A", "A", None, "A"], index=_idx(5))
    w = whipsaw(s)
    # None rows are dropped: effective run is A,A,A (one episode, no switch)
    assert w["n_switches"] == 0
    assert w["n_checks"] == 3


def test_asof_indexer_no_lookahead():
    idx = _idx(4)  # 00:00,00:05,00:10,00:15
    vals = pd.Series(["A", "B", "C", "D"], index=idx).values
    lookup = asof_indexer(idx)
    # exactly on a boundary → that row; between → previous; before first → None
    assert lookup(vals, pd.Timestamp("2026-05-01 00:10:00")) == "C"
    assert lookup(vals, pd.Timestamp("2026-05-01 00:12:00")) == "C"
    assert lookup(vals, pd.Timestamp("2026-05-01 00:04:59")) == "A"
    assert lookup(vals, pd.Timestamp("2026-04-30 23:59:00")) is None


def test_two_proportion_z_significant_gap():
    # 62.4% (951/1524) vs 56.4% (711/1260) — the probe's numbers, ~6pp, z≈3
    r = two_proportion_z(951, 1524, 711, 1260)
    assert r["delta_pp"] == 6.03 or abs(r["delta_pp"] - 6.0) < 0.2
    assert r["z"] > 3.0
    assert r["p_value"] < 0.01


def test_two_proportion_z_no_gap():
    r = two_proportion_z(50, 100, 50, 100)
    assert r["delta_pp"] == 0.0
    assert r["p_value"] > 0.5


def test_wr_by_agreement_buckets_and_shift_labels():
    rows = [
        {"soft_agrees_rule": True, "status": "CLOSED_TP", "soft_shift": None},
        {"soft_agrees_rule": True, "status": "CLOSED_SL", "soft_shift": None},
        {"soft_agrees_rule": False, "status": "CLOSED_TP", "soft_shift": "TREND_UP->CHOP"},
        {"soft_agrees_rule": False, "status": "CLOSED_SL", "soft_shift": "TREND_UP->CHOP"},
        {"soft_agrees_rule": False, "status": "CLOSED_SL", "soft_shift": "TREND_UP->CHOP"},
        {"soft_agrees_rule": None, "status": "CLOSED_TP", "soft_shift": None},  # ignored
        {"soft_agrees_rule": True, "status": "CLOSED_REGIME_CHANGE", "soft_shift": None},  # excluded, counted
    ]
    w = wr_by_agreement(rows)
    assert w["agree"]["decided"] == 2 and w["agree"]["wr_pct"] == 50.0
    assert w["disagree"]["decided"] == 3 and w["disagree"]["wr_pct"] == 33.33
    assert w["n_closed_regime_change_excluded"] == 1
    assert w["top_disagree_shifts"][0]["shift"] == "TREND_UP->CHOP"
    assert w["top_disagree_shifts"][0]["n"] == 3


def test_annotate_soft_rule_fidelity_and_agreement():
    idx = _idx(4)
    timelines = {
        "rule_recon": pd.Series(["CHOP", "CHOP", "TREND_UP", "TREND_UP"], index=idx, name="rule_recon"),
        "soft_192": pd.Series(["CHOP", "CHOP", "CHOP", "CHOP"], index=idx, name="soft_192"),
    }
    rows = [
        # recorded RULE matches rule_recon (fidelity hit), SOFT agrees
        {"ts": pd.Timestamp("2026-05-01 00:05:00"), "recorded_regime": "CHOP"},
        # at 00:15 rule_recon=TREND_UP == recorded (fidelity hit) but SOFT=CHOP → disagree
        {"ts": pd.Timestamp("2026-05-01 00:15:00"), "recorded_regime": "TREND_UP"},
        # recorded disagrees with rule_recon → fidelity miss
        {"ts": pd.Timestamp("2026-05-01 00:15:00"), "recorded_regime": "HIGH_VOLA"},
    ]
    fid = annotate_soft_rule(rows, timelines, feature_hl=192)
    assert fid["rule_recon_vs_recorded_n"] == 3
    assert fid["rule_recon_vs_recorded_agreement_pct"] == round(100 * 2 / 3, 2)
    assert rows[0]["soft_agrees_rule"] is True and rows[0]["soft_shift"] is None
    assert rows[1]["soft_agrees_rule"] is False and rows[1]["soft_shift"] == "TREND_UP->CHOP"


def test_build_timelines_soft_consumes_raw_features_frame():
    """SOFT reconstruction path runs on a raw_features-shaped frame (smoke, no DB)."""
    n = 40
    feat = pd.DataFrame(
        {
            "vola_p75": [0.35] * n,
            "vola_p40": [0.24] * n,
            "btc_return_1h": [0.1] * n,
            "btc_return_4h": [0.2] * n,
            "btc_atr_1h_pct": [0.3] * n,
            "btc_atr_4h_pct": [0.3] * n,
            "btcdom_return_24h": [None] * n,
        },
        index=_idx(n),
    )
    reg = ["CHOP"] * n
    alt = ["ALT_NEUTRAL"] * n
    tl = build_timelines(feat, reg, alt, half_lives=[48, 192])
    assert set(tl) == {"rule_recon", "raw_stored", "soft_48", "soft_192"}
    for name, s in tl.items():
        assert len(s) == n, name
    # constant features → soft labels are all the same non-None regime
    assert tl["soft_192"].notna().all()
    assert tl["soft_192"].nunique() == 1


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL  {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
