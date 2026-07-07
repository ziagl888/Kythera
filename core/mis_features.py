# core/mis_features.py — geteilter MIS1-Feature-Builder (Bot + Trainer + Simulator).
#
# Hintergrund (Report 13 / Dossier MIS1): Der Legacy-Trainer
# (legacy_trainers/X5-analyze_indicators_v8.py) und 11_ai_mis_bot.py hielten je
# eine KOPIE des Feature-Builders. Dessen `line_cols`-Schleife matchte per
# PRÄFIX über die bereits mutierten DataFrame-Spalten und erwischte dabei vier
# abgeleitete Spalten (boll_*_dist_atr, ema_200_dist_atr, ema_9_cross_above_21)
# → deren "dist_pct" ist (close − kleineZahl)/kleineZahl ≈ Coin-Preisskala →
# Ticker-/Preisklassen-Leakage, auf das die Bäume real splitteten (13-P1).
#
# Dieses Modul ist die EINE Quelle für beide Seiten (X-R-Fix "Trainer importiert
# den Feature-Builder des Bots") und behebt:
#   * line_cols jetzt EXPLIZITER Katalog der 38 rohen Indikator-Linien —
#     Präfix-Unfälle sind konstruktiv unmöglich.
#   * alle verbliebenen Preisskala-Features normalisiert: atr_14 → atr_14_pct,
#     macd_hist → macd_hist_pct, macd_dif-Delta → macd_dif_pct_delta_1.
#     Ergebnis: JEDES Feature ist skalenfrei (%, Ratio, Oszillator, Flag).
#   * Imputation identisch Bot == Trainer (P2.34-Restrisiko): erst
#     replace(±inf → NaN), dann fillna(0).
#   * Cross-Flags per-Symbol (der Legacy-Trainer shiftete rsi_14_cross_above_30 /
#     ema_9_cross_above_21 UNGRUPPIERT über Symbolgrenzen) — dieses Modul
#     arbeitet grundsätzlich auf EINEM Symbol; Multi-Coin-Frames laufen über
#     add_advanced_features_multi (groupby-apply) und sind damit exakt
#     deckungsgleich mit dem Bot-Pfad.

from __future__ import annotations

import numpy as np
import pandas as pd

# ── Roh-Spalten aus der DB (1h-Kerzen + Indikator-Join) ──────────────────────
# Der Katalog ist EXPLIZIT — neue Indikator-Spalten in der DB können nie wieder
# unbemerkt in die dist_pct-Schleife rutschen.
EMA_PERIODS = [7, 9, 12, 21, 26, 34, 50, 55, 89, 99, 200]
WMA_PERIODS = [7, 9, 12, 21, 26, 34, 50, 55, 89, 99, 200]
KAMA_PERIODS = [7, 9, 12, 21, 26, 34, 50, 55, 89, 99]

RAW_LINE_COLS = (
    [f"ema_{p}" for p in EMA_PERIODS]
    + [f"wma_{p}" for p in WMA_PERIODS]
    + [f"kama_{p}" for p in KAMA_PERIODS]
    + ["boll_upper_20", "boll_mid_20", "boll_lower_20"]
    + ["donchian_upper_20", "donchian_mid_20", "donchian_lower_20"]
)  # 38 Linien in Preisskala → je ein *_dist_pct-Feature

RSI_COLS = ["rsi_6", "rsi_9", "rsi_12", "rsi_14", "rsi_24"]

# Eingabespalten, die der Builder zwingend braucht (nach dem SQL-Aliasing
# tsi_fast_12_7_7→tsi_fast, macd_*_normal_12_26_9→macd_dif/macd_dea).
REQUIRED_INPUT_COLS = ["close", "volume"] + RSI_COLS + RAW_LINE_COLS + ["tsi_fast", "macd_dif", "macd_dea", "atr_14"]

# Gemeinsame SELECT-Liste für Bot und Simulator (h = Kerzen-Tabelle, i = Indikator-Join).
MIS_SQL_INDICATOR_SELECT = """
    i.rsi_6, i.rsi_9, i.rsi_12, i.rsi_14, i.rsi_24,
    i.ema_7, i.ema_9, i.ema_12, i.ema_21, i.ema_26, i.ema_34, i.ema_50, i.ema_55, i.ema_89, i.ema_99, i.ema_200,
    i.wma_7, i.wma_9, i.wma_12, i.wma_21, i.wma_26, i.wma_34, i.wma_50, i.wma_55, i.wma_89, i.wma_99, i.wma_200,
    i.kama_7, i.kama_9, i.kama_12, i.kama_21, i.kama_26, i.kama_34, i.kama_50, i.kama_55, i.kama_89, i.kama_99,
    i.boll_upper_20, i.boll_mid_20, i.boll_lower_20,
    i.donchian_upper_20, i.donchian_mid_20, i.donchian_lower_20,
    i.tsi_fast_12_7_7 AS tsi_fast,
    i.macd_dif_normal_12_26_9 AS macd_dif,
    i.macd_dea_normal_12_26_9 AS macd_dea,
    i.atr_14
"""

# ── Feature-Katalog (explizit, geordnet — Artefakt-meta speichert ihn mit) ───
DELTA_FEATURES = [f"{c}_delta_1" for c in RSI_COLS] + [
    "tsi_fast_delta_1",
    "macd_dif_pct_delta_1",
    "macd_hist_pct_delta_1",
]

BINARY_FLAG_FEATURES = [
    "above_ema_200",
    "rsi_14_above_50",
    "rsi_14_cross_above_30",
    "ema_9_cross_above_21",
]

FEATURE_COLS = (
    [f"{c}_dist_pct" for c in RAW_LINE_COLS]  # 38 — % Abstand Preis↔Linie
    + DELTA_FEATURES  # 8
    + ["volume_ratio_prev", "volume_ratio_sma20"]  # 2
    + RSI_COLS
    + ["tsi_fast", "macd_hist_pct"]  # 7
    + BINARY_FLAG_FEATURES  # 4
    + ["boll_upper_dist_atr", "boll_lower_dist_atr", "ema_200_dist_atr", "atr_14_pct"]  # 4
)  # = 63, alle skalenfrei

# Die 8 Legacy-Spalten, die NUR für Vergleiche mit den alten 67-Feature-pkls
# gebraucht werden (4 Unfall-Features + 4 unnormalisierte Vorgänger).
LEGACY_ONLY_COLS = [
    "boll_upper_dist_atr_dist_pct",
    "boll_lower_dist_atr_dist_pct",
    "ema_200_dist_atr_dist_pct",
    "ema_9_cross_above_21_dist_pct",
    "macd_dif_delta_1",
    "macd_hist",
    "macd_hist_delta_1",
    "atr_14",
]


def pct_distance(price_series: pd.Series, indicator_series: pd.Series) -> pd.Series:
    denominator = indicator_series.replace(0, np.nan)
    result = (price_series - indicator_series) / denominator * 100
    # P2.34: inf zuerst zu NaN — fillna(0) fängt inf nicht.
    return result.replace([np.inf, -np.inf], np.nan).fillna(0)


def add_advanced_features(df: pd.DataFrame, include_legacy: bool = False) -> pd.DataFrame:
    """Feature-Pipeline für EIN Symbol (chronologisch aufsteigend sortiert).

    `include_legacy=True` erzeugt zusätzlich die 8 LEGACY_ONLY_COLS, damit
    Retrain-Vergleiche die alten 67-Feature-Modelle exakt füttern können —
    im Live-Bot bleibt das aus.

    Fehlende Pflichtspalten sind ein harter Fehler (kein stilles fillna auf
    ganze Spalten — der P0.12-Fehlermodus).
    """
    missing = [c for c in REQUIRED_INPUT_COLS if c not in df.columns]
    if missing:
        raise ValueError(f"MIS1-Feature-Builder: Pflichtspalten fehlen: {missing}")

    df = df.copy()
    if "open_time" in df.columns:
        df = df.sort_values("open_time").reset_index(drop=True)

    for c in REQUIRED_INPUT_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    close = df["close"]

    # Volume-Ratios
    df["volume_ratio_prev"] = df["volume"] / df["volume"].shift(1)
    df["volume_sma20"] = df["volume"].rolling(20, min_periods=1).mean()
    df["volume_ratio_sma20"] = df["volume"] / df["volume_sma20"]

    # Skalenfreie MACD-Basis (Preisskala ÷ close)
    df["macd_dif_pct"] = df["macd_dif"] / close.replace(0, np.nan) * 100
    df["macd_hist_pct"] = (df["macd_dif"] - df["macd_dea"]) / close.replace(0, np.nan) * 100

    # Deltas
    for col in RSI_COLS + ["tsi_fast"]:
        df[f"{col}_delta_1"] = df[col].diff(1)
    df["macd_dif_pct_delta_1"] = df["macd_dif_pct"].diff(1)
    df["macd_hist_pct_delta_1"] = df["macd_hist_pct"].diff(1)

    # Binär-/Cross-Flags
    df["above_ema_200"] = (close > df["ema_200"]).astype(int)
    df["rsi_14_above_50"] = (df["rsi_14"] > 50).astype(int)
    df["rsi_14_cross_above_30"] = ((df["rsi_14"].shift(1) < 30) & (df["rsi_14"] >= 30)).astype(int)
    df["ema_9_cross_above_21"] = ((df["ema_9"].shift(1) < df["ema_21"].shift(1)) & (df["ema_9"] > df["ema_21"])).astype(
        int
    )

    # ATR-normalisierte Abstände + skalenfreie ATR
    eps = 1e-8
    df["boll_upper_dist_atr"] = (close - df["boll_upper_20"]) / (df["atr_14"] + eps)
    df["boll_lower_dist_atr"] = (close - df["boll_lower_20"]) / (df["atr_14"] + eps)
    df["ema_200_dist_atr"] = (close - df["ema_200"]) / (df["atr_14"] + eps)
    df["atr_14_pct"] = df["atr_14"] / close.replace(0, np.nan) * 100

    # %-Abstände NUR über den expliziten Linien-Katalog (der Leakage-Fix).
    # concat statt 38 Einzel-Inserts (pandas-Fragmentierung).
    dist = {f"{col}_dist_pct": pct_distance(close, df[col]) for col in RAW_LINE_COLS}

    if include_legacy:
        # Exakte Reproduktion der Unfall-Features des Legacy-Builders — die
        # Schleife lief dort NACH der Erzeugung der abgeleiteten Spalten.
        for col in ["boll_upper_dist_atr", "boll_lower_dist_atr", "ema_200_dist_atr", "ema_9_cross_above_21"]:
            dist[f"{col}_dist_pct"] = pct_distance(close, df[col])
        dist["macd_hist"] = df["macd_dif"] - df["macd_dea"]
        dist["macd_dif_delta_1"] = df["macd_dif"].diff(1)
        dist["macd_hist_delta_1"] = dist["macd_hist"].diff(1)

    df = pd.concat([df, pd.DataFrame(dist, index=df.index)], axis=1)

    # Imputation — MUSS in Bot, Trainer und Simulator identisch sein (P2.34).
    return df.replace([np.inf, -np.inf], np.nan).fillna(0)


def add_advanced_features_multi(df: pd.DataFrame, include_legacy: bool = False) -> pd.DataFrame:
    """Multi-Coin-Frame (Spalte `symbol`) — wendet den Builder je Symbol an,
    damit Deltas/Crosses/Rolling nie über Symbolgrenzen rechnen."""
    if "symbol" not in df.columns:
        raise ValueError("add_advanced_features_multi erwartet eine 'symbol'-Spalte")
    parts = [add_advanced_features(g, include_legacy=include_legacy) for _, g in df.groupby("symbol", sort=False)]
    return pd.concat(parts, ignore_index=True)


def assert_features_alive(df_features: pd.DataFrame, context: str = "") -> None:
    """Startup-/Trainings-Assertion "kein Feature konstant" (P0.12-Muster).

    Kontinuierliche Features müssen über die Stichprobe variieren; konstante
    Binär-Flags sind nur eine Warnung wert (legitim über kurze Fenster) und
    werden hier bewusst nicht geprüft — der Aufrufer loggt sie bei Bedarf.
    """
    missing = [c for c in FEATURE_COLS if c not in df_features.columns]
    if missing:
        raise ValueError(f"MIS1-Feature-Assertion{context}: Spalten fehlen: {missing}")
    continuous = [c for c in FEATURE_COLS if c not in BINARY_FLAG_FEATURES]
    constant = [c for c in continuous if df_features[c].nunique(dropna=False) <= 1]
    if constant:
        raise ValueError(f"MIS1-Feature-Assertion{context}: konstante Features: {constant}")
