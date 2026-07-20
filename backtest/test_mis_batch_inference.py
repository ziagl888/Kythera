# backtest/test_mis_batch_inference.py
"""Parity tests for the batched MIS2 inference (T-2026-CU-9050-186).

Bot 11 used to score every coin with a single-row `predict_proba` per model
(527 coins x 8 models = 4216 calls/scan). `_score_models_batched` collapses that
to one batched call per model. XGBoost scores rows independently, so the batched
probabilities must be byte-identical to the per-row path — these tests pin that,
plus the two things a naive vectorisation gets wrong: row-order in the
redistribution, and the failure fallback.

Run: pytest backtest/test_mis_batch_inference.py -v
"""

from __future__ import annotations

import importlib.util
import os
import sys
import unittest.mock as mock

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("DB_PASSWORD", "test")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test")


def _load_bot():
    """Load 11_ai_mis_bot.py with its heavy core.* deps mocked — we only exercise
    the pure `_score_models_batched` helper, which touches neither DB nor
    features."""
    spec = importlib.util.spec_from_file_location(
        "ai_mis_bot",
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "11_ai_mis_bot.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(
        "sys.modules",
        {
            "core.config": mock.MagicMock(),
            "core.candles": mock.MagicMock(),
            "core.charting": mock.MagicMock(),
            "core.database": mock.MagicMock(),
            "core.live_price": mock.MagicMock(),
            "core.market_utils": mock.MagicMock(),
            # core.mis_features imports only numpy/pandas — load it for real so the
            # bot's `from core.mis_features import ...` resolves the true names.
            "core.trade_utils": mock.MagicMock(),
        },
    ):
        spec.loader.exec_module(mod)
    return mod


bot = _load_bot()

FEATURES = ["f0", "f1", "f2"]


class MarkerModel:
    """`predict_proba` returns each row's `f0` as the positive-class probability.

    So the output is a fingerprint of WHICH row landed where: any row-order slip
    in the batched redistribution changes the result and the test fails.
    """

    def predict_proba(self, X: pd.DataFrame):
        p = X["f0"].to_numpy(dtype=float)
        return np.column_stack([1.0 - p, p])


class BatchHostileModel:
    """Works on a single row, raises on a multi-row batch — forces the per-row
    fallback path. Still returns `f0` as the prob so parity is checkable."""

    def predict_proba(self, X: pd.DataFrame):
        if len(X) != 1:
            raise RuntimeError("simulated batch failure")
        p = float(X["f0"].iloc[0])
        return np.array([[1.0 - p, p]])


class OneBadRowModel:
    """Raises on the batch AND on exactly one row (index-2 sentinel -1.0),
    otherwise returns `f0` — proves a single poison row becomes NaN while the
    rest score, exactly like the old per-coin try/except."""

    def predict_proba(self, X: pd.DataFrame):
        if len(X) != 1:
            raise RuntimeError("simulated batch failure")
        p = float(X["f0"].iloc[0])
        if p < 0:
            raise ValueError("simulated bad row")
        return np.array([[1.0 - p, p]])


def _frame(f0: float) -> pd.DataFrame:
    return pd.DataFrame([{"f0": f0, "f1": 0.1, "f2": 0.2}])


def _cfg(model):
    return {"model": model, "features": FEATURES, "loaded": True}


def _per_row_reference(frames, models):
    """The old path: one predict_proba per (frame, model)."""
    out = {}
    for key, cfg in models.items():
        arr = np.full(len(frames), np.nan)
        for i, f in enumerate(frames):
            try:
                arr[i] = float(cfg["model"].predict_proba(f[cfg["features"]])[0, 1])
            except Exception:
                arr[i] = np.nan
        out[key] = arr
    return out


def test_batched_equals_per_row_and_preserves_order():
    # Distinct f0 per coin so a mis-ordered redistribution cannot pass by luck.
    frames = [_frame(v) for v in (0.10, 0.25, 0.55, 0.80, 0.95)]
    models = {"8h_pump": _cfg(MarkerModel()), "24h_pump": _cfg(MarkerModel())}

    got = bot._score_models_batched(frames, models)
    ref = _per_row_reference(frames, models)

    for key in models:
        assert np.array_equal(got[key], ref[key]), f"{key}: batched != per-row"
    # Explicit order pin: each coin's own f0 comes back at its own index.
    assert got["8h_pump"].tolist() == [0.10, 0.25, 0.55, 0.80, 0.95]


def test_batch_failure_falls_back_to_per_row():
    frames = [_frame(v) for v in (0.10, 0.42, 0.77)]
    models = {"8h_pump": _cfg(BatchHostileModel())}

    got = bot._score_models_batched(frames, models)

    # Fallback still yields the exact per-row probabilities in order.
    assert got["8h_pump"].tolist() == [0.10, 0.42, 0.77]


def test_single_bad_row_becomes_nan_others_survive():
    # Middle coin (index 1) is the poison row (-1.0); neighbours must still score.
    frames = [_frame(0.30), _frame(-1.0), _frame(0.60)]
    models = {"8h_pump": _cfg(OneBadRowModel())}

    got = bot._score_models_batched(frames, models)

    assert got["8h_pump"][0] == pytest.approx(0.30)
    assert np.isnan(got["8h_pump"][1])
    assert got["8h_pump"][2] == pytest.approx(0.60)


def test_multiple_models_independent_columns():
    # Two models sharing the frame but each selecting its own feature order must
    # not cross-contaminate.
    frames = [_frame(0.2), _frame(0.9)]
    m = MarkerModel()
    models = {"8h_pump": _cfg(m), "8h_dump": {"model": m, "features": ["f2", "f0", "f1"], "loaded": True}}

    got = bot._score_models_batched(frames, models)
    # 8h_pump reads f0; 8h_dump's MarkerModel still reads column "f0" by name
    # regardless of selection order → both see the same per-row f0.
    assert got["8h_pump"].tolist() == [0.2, 0.9]
    assert got["8h_dump"].tolist() == [0.2, 0.9]


def test_empty_models_returns_empty():
    assert bot._score_models_batched([_frame(0.5)], {}) == {}
