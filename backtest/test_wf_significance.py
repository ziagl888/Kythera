# backtest/test_wf_significance.py — tests for the significance layer over
# walk-forward replay output (tools/wf_significance.py, T-2026-CU-9050-027 D3).
#
# Runs without DB and without real replay output:  python backtest/test_wf_significance.py
# (synthetic trade JSONLs; the run over a real Batch-E output is a
# VPS session — replay artifacts live only there, see docs/WF_SIGNIFICANCE.md).

import json
import os
import subprocess
import sys
import tempfile

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from tools.wf_significance import (  # noqa: E402
    analyze_group,
    max_drawdown_pct,
    order_permutation_test,
    sign_flip_control,
)


def make_trades(pnls, strategy="teststrat", direction="LONG"):
    return [
        {
            "strategy": strategy,
            "symbol": f"C{i % 7}USDT",
            "direction": direction,
            "signal_time": f"2026-01-{(i % 28) + 1:02d} {(i * 3) % 24:02d}:00:00",
            "outcome_tp1": 1 if p > 0 else 0,
            "net_pnl_pct": float(p),
            "r_multiple": float(p / 2.0),
        }
        for i, p in enumerate(pnls)
    ]


def test_edge_vs_noise_discrimination():
    """Clear edge → small p; noise → large p and Sharpe CI encloses 0."""
    rng = np.random.default_rng(1)
    edge = analyze_group(make_trades(rng.normal(0.8, 2.0, 300)), n=1000, seed=42,
                         fee_rt_pct=0.1, min_trades=20)
    noise = analyze_group(make_trades(rng.normal(0.0, 2.0, 300)), n=1000, seed=42,
                          fee_rt_pct=0.1, min_trades=20)

    assert edge["random_control"]["p_value"] < 0.01, edge["random_control"]
    assert noise["random_control"]["p_value"] > 10 * edge["random_control"]["p_value"]
    assert edge["bootstrap"]["sharpe_per_trade_ci"][0] > 0
    lo, hi = noise["bootstrap"]["sharpe_per_trade_ci"]
    assert lo < 0 < hi, (lo, hi)
    print("OK  Edge/noise: p-values and Sharpe-CIs separate cases correctly")


def test_random_control_carries_fee_drag():
    """The control is NOT a test against 0: the directionless random trader
    pays fees — with pure noise the control mean sits below 0."""
    rng = np.random.default_rng(2)
    pnls = rng.normal(0.0, 2.0, 400)
    rc = sign_flip_control(pnls, fee_rt_pct=0.1, n=1000, seed=42)
    assert rc["control_mean_pnl_pct"] < 0.0, rc
    # Expectation analytically: E[control] = 0.5*pnl + 0.5*(-pnl - 2*fee_rt) = -fee_rt,
    # CONSTANT and independent of the observed mean (review fix PR #20: previously
    # compared against mean-fee_rt — the wrong invariant).
    assert abs(rc["control_mean_pnl_pct"] - (-0.1)) < 0.05, rc
    print("OK  Random-control: fee drag in the null distribution included")


def test_order_permutation_dd():
    """Loss clustering together → observed MaxDD is atypically bad
    → SMALL p (definition: fraction of permutations that are as bad or
    worse — barely any are). Interleaved → larger p."""
    wins = [1.0] * 100
    losses = [-1.0] * 60
    clustered = np.array(losses + wins)  # all losses first → deeper DD
    res = order_permutation_test(clustered, n=500, seed=42)
    assert res["observed_max_dd_pp"] <= res["simulated_max_dd_median_pp"], res
    assert res["p_value_dd_worse"] < 0.2, res  # hardly any permutation is worse

    interleaved = np.array([v for pair in zip(wins[:60], losses) for v in pair] + wins[60:])
    res2 = order_permutation_test(interleaved, n=500, seed=42)
    assert res2["p_value_dd_worse"] > res["p_value_dd_worse"], (res2, res)
    print("OK  Order permutation: clustered losses ↔ deep MaxDD detected")


def test_max_drawdown_shape():
    assert max_drawdown_pct(np.array([1.0, 1.0, 1.0])) == 0.0
    dd = max_drawdown_pct(np.array([10.0, -5.0, -5.0, 8.0]))
    assert dd < 0.0
    # Absolute in percentage points below peak: cumsum [10,5,0,8], peak 10, deepest
    # point 0 -> -10 percentage points. Pins the unit (no longer peak-normalized).
    assert dd == -10.0, dd
    print("OK  MaxDD: 0 without drawdown, -10 percentage points with")


def test_max_drawdown_peak_height_invariance():
    """Pin against peak normalization (T-2026-CU-9050-053). Two paths with
    IDENTICAL absolute drawdown (a 50 percentage-point crash), but
    different peak heights, must yield the same MaxDD. The old
    formula (equity-peak)/peak divided by the random peak height and gave
    -25% (peak-equity 200) vs -45.45% (peak-equity 110) — exactly the
    confounding effect that flipped p_dd_worse on multi-coin replays.
    Mutation check: with the old formula this test FAILS."""
    high_peak = max_drawdown_pct(np.array([100.0, -50.0]))  # old: peak-equity 200
    low_peak = max_drawdown_pct(np.array([10.0, -50.0]))    # old: peak-equity 110
    assert high_peak == low_peak, (high_peak, low_peak)
    assert high_peak == -50.0, high_peak
    print("OK  MaxDD: peak-height-invariant (absolute percentage points, not normalized)")


def test_max_drawdown_survives_nonpositive_peak():
    """Side finding (b): if the additive equity never rises above the old 100 base,
    the peak of the old formula was <= 0 and the guard np.where(peak>0, peak, 1.0)
    switched silently to percentage points AND kept the *100 scaling — a -40 pp DD
    became -4000. The absolute DD gives cleanly -40 here, no special case.
    Mutation check: the old formula yields -4000 and FAILS."""
    # cumsum [-150,-120,-160] -> (old) 100+cumsum = [-50,-20,-60], peak <= 0 everywhere
    dd = max_drawdown_pct(np.array([-150.0, 30.0, -40.0]))
    assert dd == -40.0, dd
    print("OK  MaxDD: non-positive peak clean in percentage points, no unit/scale jump")


def test_skip_paths():
    few = analyze_group(make_trades([1.0] * 5), n=100, seed=42, fee_rt_pct=0.1, min_trades=20)
    assert "skipped" in few and few["n_closed"] == 5
    flat = analyze_group(make_trades([0.5] * 50), n=100, seed=42, fee_rt_pct=0.1, min_trades=20)
    assert "skipped" in flat  # variance 0
    open_only = analyze_group(
        [dict(t, outcome_tp1=None) for t in make_trades([1.0] * 30)],
        n=100, seed=42, fee_rt_pct=0.1, min_trades=20,
    )
    assert open_only["n_closed"] == 0 and "skipped" in open_only
    print("OK  Skip paths: too few trades / variance 0 / open trades only")


def test_cli_end_to_end_deterministic():
    """CLI via synthetic JSONL: report JSON is created, second run identical."""
    rng = np.random.default_rng(3)
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "teststrat_replay_365d.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for t in make_trades(rng.normal(0.5, 2.0, 120)):
                fh.write(json.dumps(t) + "\n")
            fh.write("{broken json\n")  # broken resume line must not crash

        script = os.path.join(REPO_ROOT, "tools", "wf_significance.py")
        runs = []
        for _ in range(2):
            r = subprocess.run([sys.executable, script, path, "--n", "300"],
                               capture_output=True, text=True)
            assert r.returncode == 0, r.stderr
            with open(os.path.splitext(path)[0] + "_significance.json", encoding="utf-8") as fh:
                runs.append(json.load(fh))
        assert runs[0]["results"] == runs[1]["results"], "CLI not deterministic (seed 42)"
        assert "teststrat" in runs[0]["results"]
        assert runs[0]["results"]["teststrat"]["n_closed"] == 120
    print("OK  CLI end-to-end: report JSON, broken line tolerated, deterministic")


if __name__ == "__main__":
    # cp1252 console (Windows): don't crash on special characters
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    test_edge_vs_noise_discrimination()
    test_random_control_carries_fee_drag()
    test_order_permutation_dd()
    test_max_drawdown_shape()
    test_max_drawdown_peak_height_invariance()
    test_max_drawdown_survives_nonpositive_peak()
    test_skip_paths()
    test_cli_end_to_end_deterministic()
    print("\nAll wf_significance tests passed.")
