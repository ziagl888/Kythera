"""Standalone, DB-free tests for tools/research/regime_switch (T-2026-KYT-9050-029).

Run:  python backtest/test_regime_switch_study.py    (or via pytest)

No network, no DB — synthetic klines only. The load-bearing test pins the in-
memory debounce PORT (timelines._step_debounce) to the REAL source
(core.regime_logic.apply_debounce) via a fake single-row regime_current conn, so
the port cannot silently drift from the live state machine.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from core.regime_logic import apply_debounce  # noqa: E402
from tools.research.regime_switch.features import build_feature_frame, feature_row  # noqa: E402
from tools.research.regime_switch.metrics import (  # noqa: E402
    common_index,
    separation_metrics,
    variant_report,
    whipsaw_metrics,
)
from tools.research.regime_switch.timelines import (  # noqa: E402
    _DebounceState,
    _step_debounce,
    build_raw_timeline,
    build_rule_timeline,
    build_soft_timeline,
)


# ─────────────────────────────────────────────────────────────────────────────
# Fake single-row regime_current conn — drives the REAL apply_debounce off-DB.
# ─────────────────────────────────────────────────────────────────────────────
class _FakeCursor:
    def __init__(self, store):
        self.store = store
        self._result = None

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("SELECT"):
            row = self.store["row"]
            self._result = None if row is None else (
                row["regime"], row["alt_context"], row["since"], row["alt_context_since"],
                row["pending_regime"], row["pending_count"],
                row["pending_alt_context"], row["pending_alt_count"],
            )
        elif s.startswith("INSERT"):
            p = params
            self.store["row"] = {
                "regime": p[0], "alt_context": p[1], "since": p[2], "alt_context_since": p[3],
                "pending_regime": None, "pending_count": 0,
                "pending_alt_context": None, "pending_alt_count": 0,
            }
        elif s.startswith("UPDATE"):
            p = params
            self.store["row"] = {
                "regime": p[0], "alt_context": p[1], "since": p[2], "alt_context_since": p[3],
                "pending_regime": p[8], "pending_count": p[9],
                "pending_alt_context": p[10], "pending_alt_count": p[11],
            }

    def fetchone(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self):
        self.store = {"row": None}

    def cursor(self):
        return _FakeCursor(self.store)

    def commit(self):
        pass


def test_debounce_port_matches_source():
    """_step_debounce (port) must produce the same effective regime sequence as the
    live apply_debounce for an identical raw stream — including the 3-check TREND
    debounce and the 2-check non-TREND debounce."""
    raw_seq = [
        ("CHOP", "ALT_NEUTRAL"),
        ("TREND_UP", "ALT_STRONG"), ("TREND_UP", "ALT_STRONG"),  # 2 checks: not yet (TREND needs 3)
        ("TREND_UP", "ALT_STRONG"),                              # 3rd → confirm TREND_UP
        ("CHOP", "ALT_STRONG"), ("CHOP", "ALT_STRONG"),          # 2 checks → confirm CHOP
        ("HIGH_VOLA", "ALT_WEAK"), ("TRANSITION", "ALT_WEAK"),   # oscillation resets pending
        ("HIGH_VOLA", "ALT_WEAK"), ("HIGH_VOLA", "ALT_WEAK"),    # 2 checks → confirm HIGH_VOLA
    ]
    conn = _FakeConn()
    st = _DebounceState()
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i, (rg, alt) in enumerate(raw_seq):
        live = apply_debounce(conn, rg, alt, 0.7, t0 + timedelta(minutes=5 * i))
        _step_debounce(st, rg, alt)
        assert st.regime == live["effective_regime"], (
            f"step {i} regime port={st.regime} live={live['effective_regime']}")
        assert st.alt == live["effective_alt_context"], (
            f"step {i} alt port={st.alt} live={live['effective_alt_context']}")
    print("  ok: debounce port matches apply_debounce over the crafted raw stream")


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic klines
# ─────────────────────────────────────────────────────────────────────────────
def _synthetic_ohlc(n=96 * 60, seed=7, drift=0.0002, vol=0.004):
    """Deterministic random-walk 15m OHLC (fixed start, no Date.now)."""
    rng = np.random.default_rng(seed)
    times = pd.date_range("2025-01-01", periods=n, freq="15min")
    steps = rng.normal(drift, vol, n)
    close = 30000 * np.exp(np.cumsum(steps))
    high = close * (1 + np.abs(rng.normal(0, vol / 2, n)))
    low = close * (1 - np.abs(rng.normal(0, vol / 2, n)))
    op = np.concatenate([[close[0]], close[:-1]])
    return pd.DataFrame({"open_time": times, "open": op, "high": high, "low": low,
                         "close": close, "volume": rng.uniform(1, 10, n)})


def test_feature_reconstruction_math():
    """A steady-uptrend series → positive 4h return, finite positive ATR%."""
    df = _synthetic_ohlc(drift=0.001, vol=0.001)
    feat = build_feature_frame(df, None)
    tail = feat.dropna(subset=["vola_p75"]).iloc[-1]
    assert tail["btc_return_4h"] > 0, "uptrend should have positive 4h return"
    assert 0 < tail["btc_atr_4h_pct"] < 100
    assert tail["vola_p75"] >= tail["vola_p40"] >= 0
    # btcdom absent → alt-context feature None (safe ALT_NEUTRAL fallback path)
    assert feature_row(tail)["btcdom_return_24h"] is None
    print("  ok: feature reconstruction math sane (returns, ATR%, percentiles, dom fallback)")


def test_feature_causality_no_lookahead():
    """Feature rows must not change when future candles are appended/removed —
    every series is causal, so truncation leaves earlier rows byte-identical."""
    df = _synthetic_ohlc()
    full = build_feature_frame(df, None)
    cut = build_feature_frame(df.iloc[: len(df) - 500], None)
    common = cut.index
    a = full.loc[common, ["btc_return_4h", "btc_atr_4h_pct"]]
    b = cut[["btc_return_4h", "btc_atr_4h_pct"]]
    # percentiles are trailing-rolling (also causal); check the raw causal cols exactly
    assert np.allclose(a.to_numpy(), b.to_numpy(), equal_nan=True), "features are not causal!"
    print("  ok: features are causal (no look-ahead) — truncation-invariant")


def test_timelines_and_metrics_shapes():
    """RAW/RULE/SOFT build over synthetic data; metrics return sane, bounded values."""
    df = _synthetic_ohlc()
    feat = build_feature_frame(df, None)
    tl = {"RAW": build_raw_timeline(feat), "RULE": build_rule_timeline(feat), "SOFT": build_soft_timeline(feat)}
    cidx = common_index(tl)
    assert len(cidx) > 96 * 20, "common window unexpectedly short"
    sliced = {k: v.reindex(cidx) for k, v in tl.items()}
    price = feat["btc_price"]

    for name, labels in sliced.items():
        w = whipsaw_metrics(labels)
        assert w["n_switches"] >= 0 and w["n_episodes"] >= 1
        sep = separation_metrics(labels, price)
        assert sep["eta_squared"] is None or (0.0 <= sep["eta_squared"] <= 1.0), "eta² out of [0,1]"
        rep = variant_report(name, labels, price)
        assert set(rep) == {"variant", "whipsaw", "trend_hold", "separation"}

    # RULE should switch no more often than RAW (debounce/hysteresis can only damp)
    assert whipsaw_metrics(sliced["RULE"])["n_switches"] <= whipsaw_metrics(sliced["RAW"])["n_switches"]
    print("  ok: timelines build; metrics bounded; RULE switches <= RAW switches")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    print(f"Running {len(fns)} tests...")
    for fn in fns:
        fn()
    print(f"\nAll {len(fns)} tests passed.")
