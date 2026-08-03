# backtest/test_bot_variant_compare.py
"""DB-free tests for tools/bot_variants/compare.py (T-2026-KYT-9050-039, D3).

The generation A/B sim over the existing replay infra:
  * evaluate(): n / avg / sum / win_rate / max_drawdown at the operating threshold
  * threshold=None ⇒ every event counts (detector gate)
  * _max_drawdown_pct on a known PnL curve
  * load_contract error paths (no feature contract)
  * compare() winner logic on a synthetic replay (no DB/network/xgboost)

Run: pytest backtest/test_bot_variant_compare.py -v
"""

from __future__ import annotations

import json
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.bot_variants import compare as cmp  # noqa: E402
from tools.retrain_from_replay import load_replay  # noqa: E402


class _StubModel:
    """predict_proba[:,1] = clamped f1 (deterministic, no xgboost)."""

    def __init__(self, feature="f1"):
        self.feature = feature

    def predict_proba(self, X):
        p = np.clip(X[self.feature].to_numpy(dtype=float), 0.0, 1.0)
        return np.column_stack([1 - p, p])


def _write_replay(path, rows):
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _replay_rows():
    # 4 events, ascending time; f1 drives prob, net_pnl_pct/outcome are set.
    times = ["2026-07-01T00:00:00Z", "2026-07-01T01:00:00Z", "2026-07-01T02:00:00Z", "2026-07-01T03:00:00Z"]
    f1s = [0.9, 0.2, 0.8, 0.1]
    pnls = [1.0, -2.0, 3.0, -5.0]
    outs = [1, 0, 1, 0]
    return [
        {
            "symbol": "BTCUSDT",
            "direction": "LONG",
            "signal_time": t,
            "outcome_tp1": o,
            "net_pnl_pct": p,
            "features": {"f1": f, "f2": 0.0},
        }
        for t, f, p, o in zip(times, f1s, pnls, outs, strict=True)
    ]


@pytest.fixture()
def replay(tmp_path):
    path = tmp_path / "demo_replay_1d.jsonl"
    _write_replay(path, _replay_rows())
    return load_replay(str(path))


def test_max_drawdown_known_curve():
    # net_pnl [+1,-2,+3,-5] → cum [1,-1,2,-3] → runmax [1,1,2,2] → dd [0,-2,0,-5]
    assert cmp._max_drawdown_pct(np.array([1.0, -2.0, 3.0, -5.0])) == -5.0
    assert cmp._max_drawdown_pct(np.array([])) == 0.0
    assert cmp._max_drawdown_pct(np.array([1.0, 2.0])) == 0.0  # monotonically rising


def test_evaluate_at_threshold(replay):
    contract = {"model": _StubModel(), "features": ["f1", "f2"], "threshold": 0.5}
    res = cmp.evaluate(contract, replay)
    # prob = f1: [0.9,0.2,0.8,0.1] ≥ 0.5 → events 0 and 2 (pnl +1, +3, both outcome=1)
    assert res["n"] == 2
    assert res["sum_net_pnl_pct"] == 4.0
    assert res["avg_net_pnl_pct"] == 2.0
    assert res["win_rate"] == 100.0
    assert res["max_drawdown_pct"] == 0.0  # +1 then +3 → no drawdown


def test_evaluate_threshold_none_takes_all(replay):
    contract = {"model": _StubModel(), "features": ["f1", "f2"], "threshold": None}
    res = cmp.evaluate(contract, replay)
    assert res["n"] == 4  # every event
    assert res["sum_net_pnl_pct"] == -3.0  # 1-2+3-5
    assert res["max_drawdown_pct"] == -5.0


def test_evaluate_override_beats_contract(replay):
    contract = {"model": _StubModel(), "features": ["f1", "f2"], "threshold": 0.5}
    res = cmp.evaluate(contract, replay, threshold=0.85)
    assert res["threshold"] == 0.85
    assert res["n"] == 1  # only f1=0.9


def test_load_contract_rejects_missing_features(tmp_path):
    import joblib

    # dict artifact without a feature list → ValueError
    p = tmp_path / "nofeat.pkl"
    joblib.dump({"model": _StubModel(), "features": []}, p)
    with pytest.raises(ValueError, match="feature"):
        cmp.load_contract(str(p))


def test_load_contract_bare_model_without_names(tmp_path):
    import joblib

    p = tmp_path / "bare.pkl"
    joblib.dump(_StubModel(), p)  # no get_booster / feature_names_in_
    with pytest.raises(ValueError, match="feature contract"):
        cmp.load_contract(str(p))


def test_compare_winner_by_avg(monkeypatch, replay, tmp_path):
    # A: threshold 0.5 (avg +2.0), B: threshold None (avg -0.75) → A wins.
    contracts = {
        "AAA": {"model": _StubModel(), "features": ["f1", "f2"], "threshold": 0.5},
        "BBB": {"model": _StubModel(), "features": ["f1", "f2"], "threshold": None},
    }
    monkeypatch.setattr(cmp, "load_contract", lambda path: contracts[path])
    monkeypatch.setattr(cmp, "resolve_artifact_path", lambda tag, direction, index=None: tag.upper())
    path = tmp_path / "demo_replay_1d.jsonl"
    _write_replay(path, _replay_rows())
    res = cmp.compare("AAA", "BBB", "LONG", str(path))
    assert res["a"]["avg_net_pnl_pct"] == 2.0
    assert res["b"]["avg_net_pnl_pct"] == -0.75
    assert res["winner_by_avg_net_pnl"] == "AAA"
    assert res["direction"] == "LONG"
    # render must not crash
    assert "AAA vs BBB" in cmp.render_compare(res)
