# backtest/test_regime_gate_edge_test.py
"""DB-freie Tests für die reine Gate-Mathematik von tools/regime_gate_edge_test.py
(Phase B, T-2026-KYT-9050-032): per-Regime-Zellen, günstige Regimes, der
out-of-sample Gate-Test (temporaler Split) und das Verdikt.

Run: pytest backtest/test_regime_gate_edge_test.py -v
     python backtest/test_regime_gate_edge_test.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")

from tools.regime_gate_edge_test import (  # noqa: E402
    favorable_regimes,
    gate_verdict,
    oos_gate_test,
    regime_cell_stats,
)


def _t(ts, regime, net):
    return {"ts": ts, "regime": regime, "net": net}


def test_regime_cell_stats():
    trades = [_t(0, "CHOP", 1.0), _t(1, "CHOP", 3.0), _t(2, "HIGH_VOLA", -2.0), _t(3, None, 5.0)]
    s = regime_cell_stats(trades)
    assert s["CHOP"] == {"n": 2, "mean_net": 2.0}
    assert s["HIGH_VOLA"] == {"n": 1, "mean_net": -2.0}
    assert None not in s  # trades ohne Regime fallen raus


def test_favorable_regimes_respects_min_cell():
    # CHOP: 20 positive -> favorable. HIGH: 20 negative -> not. TREND_UP: 5 positive
    # aber < min_cell -> nicht als favorable (zu dünn).
    trades = (
        [_t(i, "CHOP", 1.0) for i in range(20)]
        + [_t(100 + i, "HIGH_VOLA", -1.0) for i in range(20)]
        + [_t(200 + i, "TREND_UP", 2.0) for i in range(5)]
    )
    fav = favorable_regimes(trades, min_cell=20)
    assert fav == {"CHOP"}


def test_oos_gate_rescues_negative_leg():
    # Train (ts 0..39): CHOP +1, HIGH -2 -> favorable {CHOP}.
    # Test  (ts 40..79): CHOP +0.5, HIGH -1.5 -> ungated -0.5, gated (CHOP) +0.5 -> RESCUED.
    train = [_t(i, "CHOP", 1.0) for i in range(20)] + [_t(20 + i, "HIGH_VOLA", -2.0) for i in range(20)]
    test = [_t(40 + i, "CHOP", 0.5) for i in range(20)] + [_t(60 + i, "HIGH_VOLA", -1.5) for i in range(20)]
    g = oos_gate_test(train + test, min_cell=20)
    assert g["insufficient"] is False
    assert g["favorable_regimes"] == ["CHOP"]
    assert abs(g["ungated_mean_net"] - (-0.5)) < 1e-9
    assert abs(g["gated_mean_net"] - 0.5) < 1e-9
    assert abs(g["kept_frac"] - 0.5) < 1e-9
    assert gate_verdict(g) == "RESCUED"


def test_oos_gate_no_favorable_regime_blocks_all():
    # Alle Regimes negativ -> favorable leer -> Gate blockt alle Test-Trades.
    trades = [_t(i, "CHOP", -1.0) for i in range(20)] + [_t(100 + i, "HIGH_VOLA", -2.0) for i in range(20)]
    g = oos_gate_test(trades, min_cell=20)
    assert g["favorable_regimes"] == []
    assert g["gated_mean_net"] is None
    assert g["kept_frac"] == 0.0
    assert gate_verdict(g) == "NO-FAV-REGIME"


def test_oos_gate_insufficient():
    g = oos_gate_test([_t(i, "CHOP", 1.0) for i in range(10)], min_cell=20)
    assert g["insufficient"] is True
    assert gate_verdict(g) == "INSUFFICIENT"


def test_gate_verdict_labels():
    assert gate_verdict({"ungated_mean_net": 1.0, "gated_mean_net": 1.5, "kept_frac": 0.5}) == "IMPROVED"
    assert gate_verdict({"ungated_mean_net": 1.0, "gated_mean_net": 0.5, "kept_frac": 0.5}) == "WORSE"
    assert gate_verdict({"ungated_mean_net": 1.0, "gated_mean_net": 1.0, "kept_frac": 0.5}) == "NO-HELP"
    assert gate_verdict({"ungated_mean_net": -1.0, "gated_mean_net": -0.5, "kept_frac": 0.5}) == "IMPROVED"


if __name__ == "__main__":
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok  {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
