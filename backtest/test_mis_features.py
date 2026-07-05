# backtest/test_mis_features.py — Tests für den geteilten MIS1-Feature-Builder
# (core/mis_features.py, Leakage-Fix aus Report 13 / Dossier MIS1).
#
# Läuft ohne DB:  python backtest/test_mis_features.py

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
    assert len(FEATURE_COLS) == 63, f"Erwartet 63 Features, sind {len(FEATURE_COLS)}"
    assert len(set(FEATURE_COLS)) == 63, "Duplikate im Feature-Katalog"
    # Die vier Unfall-Features (13-P1) dürfen NIE im Katalog stehen:
    accidents = {
        "boll_upper_dist_atr_dist_pct", "boll_lower_dist_atr_dist_pct",
        "ema_200_dist_atr_dist_pct", "ema_9_cross_above_21_dist_pct",
    }
    assert not accidents & set(FEATURE_COLS), "Leakage-Unfall-Features im Katalog!"
    # Ebenso keine unnormalisierten Preisskala-Features:
    for banned in ("atr_14", "macd_hist", "macd_dif_delta_1", "macd_hist_delta_1"):
        assert banned not in FEATURE_COLS, f"Preisskala-Feature {banned} im Katalog!"
    print("OK  Feature-Katalog: 63 Features, keine Leakage-/Preisskala-Spalten")


def test_builder_output():
    df = add_advanced_features(make_df())
    missing = [c for c in FEATURE_COLS if c not in df.columns]
    assert not missing, f"Builder liefert Features nicht: {missing}"
    # Ohne include_legacy KEINE Unfall-Spalten erzeugen:
    for c in LEGACY_ONLY_COLS:
        assert c not in df.columns or c == "atr_14", f"Legacy-Spalte {c} ohne include_legacy erzeugt"
    X = df[FEATURE_COLS]
    assert np.isfinite(X.to_numpy(dtype=float)).all(), "inf/NaN im Feature-Output (P2.34)"
    assert_features_alive(df, context=" (Test)")
    print("OK  Builder: alle 63 Features vorhanden, finite, nicht konstant")


def test_legacy_mode():
    df = add_advanced_features(make_df(), include_legacy=True)
    missing = [c for c in LEGACY_ONLY_COLS if c not in df.columns]
    assert not missing, f"include_legacy liefert nicht alle Legacy-Spalten: {missing}"
    # Die Unfall-Features müssen Preisskala tragen (genau der Leakage-Beweis):
    acc = df["boll_upper_dist_atr_dist_pct"].abs().median()
    legit = df["ema_200_dist_pct"].abs().median()
    assert acc > 50 * max(legit, 1e-9), (
        f"Unfall-Feature nicht in Preisskala (median {acc:.1f} vs {legit:.3f}) — "
        "Legacy-Reproduktion falsch?")
    print(f"OK  Legacy-Modus: 8 Zusatzspalten, Unfall-Feature-Median {acc:.0f} vs. legit {legit:.2f}")


def test_missing_column_raises():
    df = make_df().drop(columns=["kama_21"])
    try:
        add_advanced_features(df)
    except ValueError as e:
        assert "kama_21" in str(e)
        print("OK  Fehlende Pflichtspalte → harter ValueError (kein stilles fillna)")
        return
    raise AssertionError("Fehlende Pflichtspalte wurde nicht erkannt")


def test_multi_symbol_boundary():
    """Deltas/Crosses dürfen nicht über Symbolgrenzen rechnen (Legacy-Trainer-Bug)."""
    a = make_df(seed=1, symbol="AAAUSDT")
    b = make_df(seed=2, symbol="BBBUSDT")
    multi = add_advanced_features_multi(pd.concat([a, b], ignore_index=True))
    solo_b = add_advanced_features(b.drop(columns=["symbol"]))
    got = multi[multi["symbol"] == "BBBUSDT"].reset_index(drop=True)[FEATURE_COLS]
    exp = solo_b[FEATURE_COLS]
    pd.testing.assert_frame_equal(got, exp, check_dtype=False)
    print("OK  Multi-Symbol == Solo je Symbol (keine Grenz-Leaks)")


def test_binary_flags_are_binary():
    df = add_advanced_features(make_df())
    for c in BINARY_FLAG_FEATURES:
        assert set(df[c].unique()) <= {0, 1}, f"{c} nicht binär"
    print("OK  Binär-Flags binär")


def test_required_inputs_documented():
    df = make_df()
    assert all(c in df.columns for c in REQUIRED_INPUT_COLS)
    print("OK  REQUIRED_INPUT_COLS vollständig abgedeckt vom Test-Frame")


if __name__ == "__main__":
    # cp1252-Konsole (Windows): Sonderzeichen nicht crashen lassen
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
    print("\nAlle MIS1-Feature-Builder-Tests bestanden.")
