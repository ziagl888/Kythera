# backtest/test_wf_significance.py — Tests für den Signifikanz-Layer über den
# Walk-Forward-Replay-Output (tools/wf_significance.py, T-2026-CU-9050-027 D3).
#
# Läuft ohne DB und ohne echten Replay-Output:  python backtest/test_wf_significance.py
# (synthetische Trade-JSONLs; der Lauf über einen echten Batch-E-Output ist eine
# VPS-Session — Replay-Artefakte liegen nur dort, siehe docs/WF_SIGNIFICANCE.md).

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
    """Klarer Edge → kleines p; Rauschen → großes p und Sharpe-CI umschließt 0."""
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
    print("OK  Edge/Rauschen: p-Werte und Sharpe-CIs trennen die Fälle korrekt")


def test_random_control_carries_fee_drag():
    """Die Kontrolle ist KEIN Test gegen 0: der richtungslose Zufalls-Trader
    zahlt Fees — bei reinem Rauschen liegt der Kontroll-Mittelwert unter 0."""
    rng = np.random.default_rng(2)
    pnls = rng.normal(0.0, 2.0, 400)
    rc = sign_flip_control(pnls, fee_rt_pct=0.1, n=1000, seed=42)
    assert rc["control_mean_pnl_pct"] < 0.0, rc
    # Erwartung analytisch: E[control] = 0.5*pnl + 0.5*(-pnl - 2*fee_rt) = -fee_rt,
    # KONSTANT und unabhängig vom beobachteten Mittel (Review-Fix PR #20: vorher
    # wurde gegen mean-fee_rt verglichen — die falsche Invariante).
    assert abs(rc["control_mean_pnl_pct"] - (-0.1)) < 0.05, rc
    print("OK  Random-Control: Fee-Drag in der Null-Verteilung enthalten")


def test_order_permutation_dd():
    """Verlust-Clusterung am Stück → beobachteter MaxDD ist untypisch schlecht
    → KLEINES p (Definition: Anteil Permutationen, die gleich schlecht oder
    schlechter sind — kaum eine ist es). Interleaved → größeres p."""
    wins = [1.0] * 100
    losses = [-1.0] * 60
    clustered = np.array(losses + wins)  # alle Verluste zuerst → tiefer DD
    res = order_permutation_test(clustered, n=500, seed=42)
    assert res["observed_max_dd_pct"] <= res["simulated_max_dd_median_pct"], res
    assert res["p_value_dd_worse"] < 0.2, res  # kaum eine Permutation ist schlechter

    interleaved = np.array([v for pair in zip(wins[:60], losses) for v in pair] + wins[60:])
    res2 = order_permutation_test(interleaved, n=500, seed=42)
    assert res2["p_value_dd_worse"] > res["p_value_dd_worse"], (res2, res)
    print("OK  Reihenfolge-Permutation: geclusterte Verluste ↔ tiefer MaxDD erkannt")


def test_max_drawdown_shape():
    assert max_drawdown_pct(np.array([1.0, 1.0, 1.0])) == 0.0
    dd = max_drawdown_pct(np.array([10.0, -5.0, -5.0, 8.0]))
    assert dd < 0.0
    print("OK  MaxDD: 0 ohne Drawdown, negativ mit")


def test_skip_paths():
    few = analyze_group(make_trades([1.0] * 5), n=100, seed=42, fee_rt_pct=0.1, min_trades=20)
    assert "skipped" in few and few["n_closed"] == 5
    flat = analyze_group(make_trades([0.5] * 50), n=100, seed=42, fee_rt_pct=0.1, min_trades=20)
    assert "skipped" in flat  # Varianz 0
    open_only = analyze_group(
        [dict(t, outcome_tp1=None) for t in make_trades([1.0] * 30)],
        n=100, seed=42, fee_rt_pct=0.1, min_trades=20,
    )
    assert open_only["n_closed"] == 0 and "skipped" in open_only
    print("OK  Skip-Pfade: zu wenig Trades / Varianz 0 / nur offene Trades")


def test_cli_end_to_end_deterministic():
    """CLI über eine synthetische JSONL: Report-JSON entsteht, zweiter Lauf identisch."""
    rng = np.random.default_rng(3)
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "teststrat_replay_365d.jsonl")
        with open(path, "w", encoding="utf-8") as fh:
            for t in make_trades(rng.normal(0.5, 2.0, 120)):
                fh.write(json.dumps(t) + "\n")
            fh.write("{broken json\n")  # abgebrochene resume-Zeile darf nicht crashen

        script = os.path.join(REPO_ROOT, "tools", "wf_significance.py")
        runs = []
        for _ in range(2):
            r = subprocess.run([sys.executable, script, path, "--n", "300"],
                               capture_output=True, text=True)
            assert r.returncode == 0, r.stderr
            with open(os.path.splitext(path)[0] + "_significance.json", encoding="utf-8") as fh:
                runs.append(json.load(fh))
        assert runs[0]["results"] == runs[1]["results"], "CLI nicht deterministisch (Seed 42)"
        assert "teststrat" in runs[0]["results"]
        assert runs[0]["results"]["teststrat"]["n_closed"] == 120
    print("OK  CLI end-to-end: Report-JSON, kaputte Zeile toleriert, deterministisch")


if __name__ == "__main__":
    # cp1252-Konsole (Windows): Sonderzeichen nicht crashen lassen
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    test_edge_vs_noise_discrimination()
    test_random_control_carries_fee_drag()
    test_order_permutation_dd()
    test_max_drawdown_shape()
    test_skip_paths()
    test_cli_end_to_end_deterministic()
    print("\nAlle wf_significance-Tests bestanden.")
