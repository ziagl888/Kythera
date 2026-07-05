"""
core/aim2_features.py — geteilter Feature-Builder für AIM2 (Trainer UND Serving).

Der AIM1-Vorgänger hatte drei tödliche Skews zwischen Training und Live
(round-vs-floor-Join, totes One-Hot-Vokabular, Selbst-Feedback). Deshalb gilt
hier das MIS1-Muster (Commit e84bc7d): tools/aim2_build_dataset.py und
15_ai_master_bot.py bauen jede Feature-Zeile über exakt diese eine Funktion.

Vertrag:
  * market_row  = Indikator-Zeile der letzten GESCHLOSSENEN 1h-Kerze vor dem
    Event (floor-1-Join; der Aufrufer ist für `open_time < floor(event)`
    verantwortlich), close = Close derselben Kerze.
  * regime_row  = jüngste regime_history-Zeile mit ts <= Event (UTC!), None ok.
  * swarm       = 5d-Schwarm-Kontext OHNE AIM1/AIM2 und OHNE das Event selbst.
  * source      = Quellsignal-Identität; das One-Hot-Vokabular entsteht beim
    Training aus den Daten (Artefakt-Featureliste), NICHT aus dieser Datei.

Absolute Preis-/Skalen-Features sind bewusst verboten (Ticker-Leakage,
Report 13).
"""

from __future__ import annotations

import math

# Preis-basierte Indikatoren → Distanz zum Close in % (skalenfrei)
MARKET_PRICE_COLS = [
    "ema_9", "ema_21", "ema_50", "ema_200",
    "kama_21", "wma_21",
    "boll_upper_20", "boll_mid_20", "boll_lower_20",
    "donchian_upper_20", "donchian_lower_20", "donchian_mid_20",
    "support_price", "resistance_price", "trendline_price",
]

# Bereits skalenfreie Indikatoren → unverändert
MARKET_ABS_COLS = [
    "rsi_6", "rsi_14",
    "tsi_25_13_13",
    "macd_dif_normal_12_26_9", "macd_dea_normal_12_26_9",
    "trendline_slope", "r_squared",
]

ATR_COLS = ["atr_14", "atr_21"]  # → % vom Close

TREND_VALUES = ["UP", "DOWN", "SIDEWAYS"]
REGIME_VALUES = ["TREND_UP", "TREND_DOWN", "CHOP", "HIGH_VOLA", "TRANSITION"]
ALT_VALUES = ["ALT_STRONG", "ALT_NEUTRAL", "ALT_WEAK"]

# Conv-Signale haben keine Modell-Confidence — Mapping wie Bot 15 (AIM1-Ära),
# damit source_conf über den Bruch AIM1→AIM2 vergleichbar bleibt.
CONV_CONFIDENCE_MAPPING = {
    "Fast In And Out": 0.65,
    "Fast Bot": 0.65,
    "5 Percent": 0.80,
    "5% Bot": 0.80,
    "Volume Indicator": 0.60,
    "Volume Bot": 0.60,
    "Support Resistance": 0.60,
    "SR Bot": 0.60,
    "Main Channel": 0.55,
}

# Trailing-WR-Semantik (closed_ai_signals) — MUSS in Trainer und Serving
# identisch sein: Win = irgendein Target getroffen.
TRAIL_WIN_SQL = "(status ILIKE '%%TARGET%%' OR COALESCE(targets_hit, 0) >= 1)"
TRAIL_WINDOW_DAYS = 30


def _f(value, default: float = 0.0) -> float:
    """Robuste float-Konvertierung; NaN/inf/None/Fehler → default."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(v):
        return default
    return v


def build_feature_row(
    market_row: dict,
    close: float,
    regime_row: dict | None,
    regime_age_min: float,
    swarm: dict,
    source: dict,
) -> dict:
    """Eine Event-Zeile → flaches Feature-Dict (nur endliche floats).

    source-Keys: name, type ('ai'|'conv'), conf, trail_wr_30d, trail_n_30d,
    entry_drift_pct, direction ('LONG'|'SHORT').
    swarm-Keys: total_5d, long_5d, short_5d, latest_age_h,
    confl_same_dir_4h, distinct_src_same_dir_4h.
    """
    row: dict[str, float] = {}
    close_safe = _f(close)
    if close_safe <= 0:
        close_safe = 1.0

    # --- Markt (floor-1-Kerze) ---
    for col in MARKET_PRICE_COLS:
        val = _f(market_row.get(col), default=float("nan"))
        row[f"{col}_dist_pct"] = (
            (val - close_safe) / close_safe * 100.0 if math.isfinite(val) and val > 0 else 0.0
        )
    for col in MARKET_ABS_COLS:
        row[col] = _f(market_row.get(col))
    for col in ATR_COLS:
        row[f"{col}_pct_close"] = _f(market_row.get(col)) / close_safe * 100.0

    trend = str(market_row.get("trend_direction") or "nan")
    for t in TREND_VALUES:
        row[f"trend_{t}"] = 1.0 if trend == t else 0.0

    # --- Regime (der 2025 fehlende Prädiktor) ---
    regime = str((regime_row or {}).get("regime") or "nan")
    alt = str((regime_row or {}).get("alt_context") or "nan")
    for r in REGIME_VALUES:
        row[f"regime_{r}"] = 1.0 if regime == r else 0.0
    for a in ALT_VALUES:
        row[f"alt_{a}"] = 1.0 if alt == a else 0.0
    rr = regime_row or {}
    row["regime_confidence"] = _f(rr.get("confidence"))
    row["regime_confidence_btc"] = _f(rr.get("confidence_btc"))
    row["regime_confidence_alt"] = _f(rr.get("confidence_alt"))
    row["btc_return_1h"] = _f(rr.get("btc_return_1h"))
    row["btc_return_4h"] = _f(rr.get("btc_return_4h"))
    row["btc_atr_1h_pct"] = _f(rr.get("btc_atr_1h_pct"))
    row["btc_atr_4h_pct"] = _f(rr.get("btc_atr_4h_pct"))
    row["btcdom_return_24h"] = _f(rr.get("btcdom_return_24h"))
    # Staleness gekappt: >6h heißt „Regime-Info praktisch fehlend"
    row["regime_age_min"] = min(_f(regime_age_min, default=360.0), 360.0)

    # --- Schwarm (ohne AIM1/AIM2, ohne das Event selbst — F6-Fix) ---
    total = _f(swarm.get("total_5d"))
    longs = _f(swarm.get("long_5d"))
    shorts = _f(swarm.get("short_5d"))
    row["swarm_total_5d"] = total
    row["swarm_long_5d"] = longs
    row["swarm_short_5d"] = shorts
    denom = longs + shorts
    row["swarm_long_prob_5d"] = longs / denom if denom > 0 else 0.5
    row["swarm_latest_age_h"] = min(_f(swarm.get("latest_age_h"), default=120.0), 120.0)
    row["swarm_confl_same_dir_4h"] = _f(swarm.get("confl_same_dir_4h"))
    row["swarm_distinct_src_same_dir_4h"] = _f(swarm.get("distinct_src_same_dir_4h"))

    # --- Quelle ---
    name = str(source.get("name") or "nan").strip()
    row[f"src_{name}"] = 1.0  # Vokabular entsteht im Training (Artefakt-Liste)
    row["src_is_ai"] = 1.0 if source.get("type") == "ai" else 0.0
    row["src_conf"] = _f(source.get("conf"))
    row["src_trail_wr_30d"] = _f(source.get("trail_wr_30d"), default=0.5)
    row["src_trail_n_30d"] = math.log1p(max(_f(source.get("trail_n_30d")), 0.0))
    row["entry_drift_pct"] = _f(source.get("entry_drift_pct"))
    row["direction_num"] = 1.0 if str(source.get("direction", "")).upper() == "LONG" else 0.0

    return row


def parity_nonzero_share(vector, feature_names) -> float:
    """Anteil der Nicht-Null-Features — OOD-Wache gegen den P0.13-Fehlermodus
    (Serving-reindex nullt still das halbe Vokabular)."""
    nonzero = sum(1 for v in vector if _f(v) != 0.0)
    return nonzero / max(len(feature_names), 1)
