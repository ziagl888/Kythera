"""DB-freie Tests für core.ats_features — der geteilte ATS/TSI-Feature-Builder
von Bot 12 und dem ATS2-Walkforward-Adapter/Trainer.

KERN (harte Regel 7): der Parity-Test beweist, dass
core.ats_features.build_ats_features BIT-GLEICH das reproduziert, was Bot 12
VOR T-2026-CU-9050-121 inline gebaut hat (`_serving_reference` unten ist eine
wortwörtliche Kopie dieser Serving-Konstruktion). Damit gilt Trainer == Serving:
der Bot ruft build_ats_features, der Trainer ruft build_ats_features, und beide
== die historische Live-Semantik.

Kein DB-Zugriff — läuft standalone (kein pytest-Plugin nötig für die Kern-
Asserts; die Monkeypatch-Tests brauchen pytest).
"""

from __future__ import annotations

import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.ats_features import (  # noqa: E402
    ATS_CANDLE_COLUMNS,
    ATS_FEATURES,
    ATS_INDICATOR_COLUMNS,
    assert_features_alive,
    ats_cross,
    build_ats_features,
)

ALL_COLUMNS = list(ATS_CANDLE_COLUMNS) + list(ATS_INDICATOR_COLUMNS)


# --------------------------------------------------------------------------- #
# Fixture-Generator                                                            #
# --------------------------------------------------------------------------- #
def make_ats_frame(n: int = 120, seed: int = 0, cross_at: int | None = None, cross_dir: str = "LONG") -> pd.DataFrame:
    """Deterministisches 1h-Fenster mit ALLEN von Bot 12 gelesenen Spalten.

    Werte müssen nicht TA-konsistent sein — der Feature-Builder rechnet reine
    Arithmetik auf den Spalten. Sie sind nur finit und variierend, damit
    Features nicht degenerieren. `cross_at`/`cross_dir` erzwingen einen
    TSI-Signallinien-Crossover an einem Index (für den Adapter-Smoke-Test).
    """
    rng = np.random.default_rng(seed)
    t0 = pd.Timestamp("2026-01-01", tz="UTC")
    times = [t0 + pd.Timedelta(hours=i) for i in range(n)]
    close = 100.0 + np.cumsum(rng.normal(0, 1.0, n))
    close = np.abs(close) + 10.0
    high = close + rng.uniform(0.1, 2.0, n)
    low = close - rng.uniform(0.1, 2.0, n)
    volume = rng.uniform(1000.0, 5000.0, n)

    df = pd.DataFrame(
        {
            "open_time": times,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    )

    # Indikatorspalten: plausible, variierende, finite Werte.
    df["rsi_14"] = rng.uniform(20, 80, n)
    df["rsi_6"] = rng.uniform(20, 80, n)
    df["tsi_fast_12_7_7"] = rng.normal(0, 20, n)
    df["tsi_fast_12_7_7_signal"] = rng.normal(0, 20, n)
    df["ema_9"] = close * rng.uniform(0.98, 1.02, n)
    df["ema_21"] = close * rng.uniform(0.97, 1.03, n)
    df["ema_50"] = close * rng.uniform(0.95, 1.05, n)
    df["ema_200"] = close * rng.uniform(0.90, 1.10, n)
    df["kama_9"] = close * rng.uniform(0.98, 1.02, n)
    df["kama_21"] = close * rng.uniform(0.97, 1.03, n)
    df["kama_55"] = close * rng.uniform(0.95, 1.05, n)
    df["macd_dif_normal_12_26_9"] = rng.normal(0, 1.0, n)
    df["macd_dea_normal_12_26_9"] = rng.normal(0, 1.0, n)
    df["atr_14"] = rng.uniform(0.5, 3.0, n)
    df["boll_upper_20"] = close + rng.uniform(2, 5, n)
    df["boll_lower_20"] = close - rng.uniform(2, 5, n)
    df["donchian_upper_20"] = high + rng.uniform(0.5, 2, n)
    df["donchian_lower_20"] = low - rng.uniform(0.5, 2, n)
    df["trendline_slope"] = rng.normal(0, 0.5, n)
    df["support_price"] = low - rng.uniform(1, 3, n)
    df["resistance_price"] = high + rng.uniform(1, 3, n)

    if cross_at is not None:
        # Signallinie flach bei 0; TSI wechselt am Index das Vorzeichen → Cross.
        df["tsi_fast_12_7_7_signal"] = 0.0
        df.loc[: cross_at - 1, "tsi_fast_12_7_7"] = -5.0
        df.loc[cross_at:, "tsi_fast_12_7_7"] = 5.0
        if cross_dir == "SHORT":
            df["tsi_fast_12_7_7"] = -df["tsi_fast_12_7_7"]

    return df


# --------------------------------------------------------------------------- #
# GROUND TRUTH: wortwörtliche Kopie der Bot-12-Serving-Konstruktion            #
# (12_ai_ats_bot.check_tsi_crossovers, Stand VOR T-2026-CU-9050-121).          #
# --------------------------------------------------------------------------- #
def _serving_reference(df: pd.DataFrame) -> dict:
    df = df.copy()
    obv_raw = (np.sign(df['close'].diff()) * df['volume']).fillna(0).cumsum()
    df['obv'] = obv_raw - obv_raw.iloc[0]
    df['typical_price'] = (df['high'] + df['low'] + df['close']) / 3
    df['vwap_20'] = (df['volume'] * df['typical_price']).rolling(20).sum() / df['volume'].rolling(20).sum()
    df['vwap_20'] = df['vwap_20'].fillna(df['close'])

    current_idx = -1
    prev_idx = -2
    row = df.iloc[current_idx]
    row_prev = df.iloc[prev_idx]

    vol_sma20 = df['volume'].rolling(20).mean().iloc[current_idx]
    if vol_sma20 == 0:
        vol_sma20 = 1.0

    return {
        "rsi_14": row['rsi_14'],
        "rsi_6": row['rsi_6'],
        "macd_hist": row['macd_dif_normal_12_26_9'] - row['macd_dea_normal_12_26_9'],
        "atr_pct": (row['atr_14'] / row['close']) * 100 if row['close'] else 0,
        "vol_ratio": row['volume'] / vol_sma20,
        "bb_width": (row['boll_upper_20'] - row['boll_lower_20']) / row['boll_lower_20'] if row['boll_lower_20'] else 0,
        "bb_pos": (row['close'] - row['boll_lower_20']) / (row['boll_upper_20'] - row['boll_lower_20'])
        if (row['boll_upper_20'] - row['boll_lower_20']) != 0
        else 0,
        "dist_ema200": (row['close'] / row['ema_200']) - 1 if row['ema_200'] else 0,
        "dist_ema9_21": (row['ema_9'] / row['ema_21']) - 1 if row['ema_21'] else 0,
        "dist_kama9": (row['close'] / row['kama_9']) - 1 if row['kama_9'] else 0,
        "dist_kama21": (row['close'] / row['kama_21']) - 1 if row['kama_21'] else 0,
        "dist_kama55": (row['close'] / row['kama_55']) - 1 if row['kama_55'] else 0,
        "dist_kama9_21": (row['kama_9'] / row['kama_21']) - 1 if row['kama_21'] else 0,
        "dist_donch_up": (row['close'] / row['donchian_upper_20']) - 1 if row['donchian_upper_20'] else 0,
        "dist_donch_low": (row['close'] / row['donchian_lower_20']) - 1 if row['donchian_lower_20'] else 0,
        "rsi_ratio": row['rsi_6'] / row['rsi_14'] if row['rsi_14'] else 0,
        "slope_norm": (row['trendline_slope'] / row['close']) * 1000 if row['close'] else 0,
        "dist_supp": (row['close'] - row['support_price']) / row['close'] if row['close'] else 0,
        "dist_res": (row['resistance_price'] - row['close']) / row['close'] if row['close'] else 0,
        "macd_cross_bearish": int(
            row_prev['macd_dif_normal_12_26_9'] >= row_prev['macd_dea_normal_12_26_9']
            and row['macd_dif_normal_12_26_9'] < row['macd_dea_normal_12_26_9']
        ),
        "ema9_21_cross_bearish": int(row_prev['ema_9'] >= row_prev['ema_21'] and row['ema_9'] < row['ema_21']),
        "kama9_21_cross_bearish": int(row_prev['kama_9'] >= row_prev['kama_21'] and row['kama_9'] < row['kama_21']),
        "bollinger_lower_break": int(row['close'] < row['boll_lower_20']),
        "close_below_ema50": int(row['close'] < row['ema_50']),
        "obv_ratio": row['obv'] / df['obv'].rolling(20).mean().iloc[current_idx]
        if df['obv'].rolling(20).mean().iloc[current_idx] != 0
        else 0,
        "close_to_vwap_pct": (row['close'] / row['vwap_20']) - 1 if row['vwap_20'] else 0,
        "obv_val": row['obv'],
        "volume_spike": int(row['volume'] > vol_sma20 * 2),
        "volume_trend_up": int(df['volume'].rolling(5).mean().iloc[current_idx] > vol_sma20),
    }


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #
def test_parity_trainer_equals_serving():
    """DER Kern-Test (harte Regel 7): build_ats_features == frühere Bot-12-Inline-
    Konstruktion, über mehrere Seeds UND mehrere Fensterlängen (die OBV-Baseline
    hängt vom Fensterstart ab — genau der Punkt, den der ATS2-Trainer treffen
    muss)."""
    for seed in range(6):
        base = make_ats_frame(n=140, seed=seed)
        for win_len in (50, 90, 140):
            window = base.iloc[:win_len].reset_index(drop=True)
            got = build_ats_features(window)
            ref = _serving_reference(window)
            assert set(got.keys()) == set(ref.keys()) == set(ATS_FEATURES)
            for k in ATS_FEATURES:
                assert math.isclose(float(got[k]), float(ref[k]), rel_tol=1e-12, abs_tol=1e-12), (
                    f"Parität verletzt bei {k} (seed={seed}, win={win_len}): {got[k]} != {ref[k]}"
                )


def test_feature_keys_and_native_types():
    """Der Vertrag ist die Feature-MENGE: sowohl der Bot (X_live[TSI_FEATURES])
    als auch der Trainer (train[ATS2_FEATURES]) selektieren die Spalten NACH
    NAMEN und erzwingen so die Reihenfolge — die dict-Insertion-Order ist egal.
    Geprüft: exakte Schlüsselmenge, verlustfreie Reindizierung, native Typen."""
    feats = build_ats_features(make_ats_frame(n=80, seed=1))
    assert set(feats.keys()) == set(ATS_FEATURES), "Feature-Schlüsselmenge weicht vom Vertrag ab"
    # Reindex nach dem Vertrag darf keine Spalte als NaN erfinden (= fehlender Key).
    reindexed = pd.DataFrame([feats]).reindex(columns=ATS_FEATURES)
    assert not reindexed.isna().any().any(), "Reindex nach ATS_FEATURES erzeugt NaN → fehlender Feature-Key"
    binary = {
        "macd_cross_bearish",
        "ema9_21_cross_bearish",
        "kama9_21_cross_bearish",
        "bollinger_lower_break",
        "close_below_ema50",
        "volume_spike",
        "volume_trend_up",
    }
    for k, v in feats.items():
        assert type(v) in (int, float), f"{k} ist kein natives int/float: {type(v)}"
        assert np.isfinite(v), f"{k} nicht endlich: {v}"
        if k in binary:
            assert v in (0, 1), f"Flag {k} nicht 0/1: {v}"


def test_ats_cross_directions():
    assert ats_cross(-1.0, 0.0, 1.0, 0.0) == "LONG"  # von unten nach oben
    assert ats_cross(1.0, 0.0, -1.0, 0.0) == "SHORT"  # von oben nach unten
    assert ats_cross(1.0, 0.0, 2.0, 0.0) is None  # beide über Signal, kein Cross
    assert ats_cross(-2.0, 0.0, -1.0, 0.0) is None  # beide unter Signal
    # Berührung von unten zählt als LONG (<=), Berührung von oben als SHORT (>=)
    assert ats_cross(0.0, 0.0, 1.0, 0.0) == "LONG"


def test_assert_features_alive_guard():
    import pytest

    rows = [build_ats_features(make_ats_frame(n=120, seed=s)) for s in range(5)]
    feat_df = pd.DataFrame(rows)
    assert_features_alive(feat_df)  # variierende Fixtures → darf nicht werfen

    with pytest.raises(ValueError):
        assert_features_alive(feat_df.drop(columns=["rsi_14"]))  # fehlende Spalte
    with pytest.raises(ValueError):
        assert_features_alive(pd.DataFrame([rows[0], rows[0]]))  # alles konstant


def test_run_ats_adapter_emits_record(monkeypatch):
    """DB-freier End-to-End-Test des Walkforward-Adapters: load_ats_frame wird
    gestubbt, get_hvn_and_sr_levels (df=-Variante) und simulate_exit laufen echt."""
    import tools.walkforward_sim as w

    df = make_ats_frame(n=600, seed=3, cross_at=300, cross_dir="LONG")
    monkeypatch.setattr(w, "load_ats_frame", lambda conn, sym, days: df.copy())
    trades = w.run_ats(conn=None, symbol="TESTUSDT", days=365)

    assert len(trades) >= 1, "Adapter emittierte keinen Crossover"
    tr = trades[0]
    assert tr["strategy"] == "ats" and tr["direction"] == "LONG"
    assert set(tr["features"].keys()) == set(ATS_FEATURES)
    assert "outcome_tp1" in tr and "net_pnl_pct" in tr
    assert tr["targets"] and tr["sl"] > 0

    # Replay-Feature == Serving-Feature am selben Entscheidungspunkt (t=300):
    # der Adapter reicht df.iloc[t+1-500 : t+1] durch (hier ab 0, weil <500 Kerzen).
    window = df.iloc[: 300 + 1].reset_index(drop=True)
    ref = _serving_reference(window)
    for k in ATS_FEATURES:
        assert math.isclose(float(tr["features"][k]), float(ref[k]), rel_tol=1e-12, abs_tol=1e-12)


if __name__ == "__main__":
    test_parity_trainer_equals_serving()
    test_feature_keys_and_native_types()
    test_ats_cross_directions()
    print("✅ ats_features Kern-Paritäts-Tests grün (Monkeypatch-Tests via pytest).")
