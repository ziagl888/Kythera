# backtest/test_mis_features.py — Tests for the shared MIS1 feature builder
# (core/mis_features.py, leakage fix from Report 13 / Dossier MIS1).
#
# Runs without DB:  python backtest/test_mis_features.py

import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.mis_features import (  # noqa: E402
    BINARY_FLAG_FEATURES,
    FEATURE_COLS,
    LEGACY_ONLY_COLS,
    RAW_LINE_COLS,
    REQUIRED_INPUT_COLS,
    add_advanced_features,
    add_advanced_features_multi,
    assert_features_alive,
)


def make_df(n=300, seed=7, symbol=None) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    df = pd.DataFrame({
        "open_time": pd.date_range("2026-01-01", periods=n, freq="h", tz="UTC"),
        "close": close,
        "volume": rng.uniform(1000, 50000, n),
    })
    for c in ["rsi_6", "rsi_9", "rsi_12", "rsi_14", "rsi_24"]:
        df[c] = rng.uniform(20, 80, n)
    for c in RAW_LINE_COLS:
        df[c] = close * rng.uniform(0.97, 1.03, n)
    df["tsi_fast"] = rng.normal(0, 20, n)
    df["macd_dif"] = rng.normal(0, 0.5, n) * close / 100
    df["macd_dea"] = rng.normal(0, 0.5, n) * close / 100
    df["atr_14"] = close * rng.uniform(0.005, 0.03, n)
    if symbol:
        df["symbol"] = symbol
    return df


def test_feature_catalog():
    assert len(FEATURE_COLS) == 63, f"Expected 63 features, got {len(FEATURE_COLS)}"
    assert len(set(FEATURE_COLS)) == 63, "Duplicates in feature catalog"
    # The four accident features (13-P1) must NEVER be in the catalog:
    accidents = {
        "boll_upper_dist_atr_dist_pct", "boll_lower_dist_atr_dist_pct",
        "ema_200_dist_atr_dist_pct", "ema_9_cross_above_21_dist_pct",
    }
    assert not accidents & set(FEATURE_COLS), "Leakage accident features in catalog!"
    # Also no unnormalised price-scale features:
    for banned in ("atr_14", "macd_hist", "macd_dif_delta_1", "macd_hist_delta_1"):
        assert banned not in FEATURE_COLS, f"Price-scale feature {banned} in catalog!"
    print("OK  Feature catalog: 63 features, no leakage / price-scale columns")


def test_builder_output():
    df = add_advanced_features(make_df())
    missing = [c for c in FEATURE_COLS if c not in df.columns]
    assert not missing, f"Builder does not deliver features: {missing}"
    # Without include_legacy, do NOT generate accident columns:
    for c in LEGACY_ONLY_COLS:
        assert c not in df.columns or c == "atr_14", f"Legacy column {c} generated without include_legacy"
    X = df[FEATURE_COLS]
    assert np.isfinite(X.to_numpy(dtype=float)).all(), "inf/NaN in feature output (P2.34)"
    assert_features_alive(df, context=" (Test)")
    print("OK  Builder: all 63 features present, finite, not constant")


def test_legacy_mode():
    df = add_advanced_features(make_df(), include_legacy=True)
    missing = [c for c in LEGACY_ONLY_COLS if c not in df.columns]
    assert not missing, f"include_legacy does not deliver all legacy columns: {missing}"
    # The accident features must carry price-scale (precisely the leakage proof):
    acc = df["boll_upper_dist_atr_dist_pct"].abs().median()
    legit = df["ema_200_dist_pct"].abs().median()
    assert acc > 50 * max(legit, 1e-9), (
        f"Accident feature not in price-scale (median {acc:.1f} vs {legit:.3f}) — "
        "legacy reproduction wrong?")
    print(f"OK  Legacy mode: 8 additional columns, accident feature median {acc:.0f} vs. legit {legit:.2f}")


def test_missing_column_raises():
    df = make_df().drop(columns=["kama_21"])
    try:
        add_advanced_features(df)
    except ValueError as e:
        assert "kama_21" in str(e)
        print("OK  Missing required column → hard ValueError (no silent fillna)")
        return
    raise AssertionError("Missing required column was not recognised")


def test_multi_symbol_boundary():
    """Deltas/crosses must not compute across symbol boundaries (legacy trainer bug)."""
    a = make_df(seed=1, symbol="AAAUSDT")
    b = make_df(seed=2, symbol="BBBUSDT")
    multi = add_advanced_features_multi(pd.concat([a, b], ignore_index=True))
    solo_b = add_advanced_features(b.drop(columns=["symbol"]))
    got = multi[multi["symbol"] == "BBBUSDT"].reset_index(drop=True)[FEATURE_COLS]
    exp = solo_b[FEATURE_COLS]
    pd.testing.assert_frame_equal(got, exp, check_dtype=False)
    print("OK  Multi-symbol == solo per symbol (no boundary leaks)")


def test_binary_flags_are_binary():
    df = add_advanced_features(make_df())
    for c in BINARY_FLAG_FEATURES:
        assert set(df[c].unique()) <= {0, 1}, f"{c} not binary"
    print("OK  Binary flags binary")


def test_required_inputs_documented():
    df = make_df()
    assert all(c in df.columns for c in REQUIRED_INPUT_COLS)
    print("OK  REQUIRED_INPUT_COLS fully covered by test frame")


if __name__ == "__main__":
    # cp1252 console (Windows): special characters must not crash
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    test_feature_catalog()
    test_builder_output()
    test_legacy_mode()
    test_missing_column_raises()
    test_multi_symbol_boundary()
    test_binary_flags_are_binary()
    test_required_inputs_documented()
    print("\nAll MIS1 feature builder tests passed.")
