"""
core/aim2_features.py — shared feature builder for AIM2 (trainer AND serving).

The AIM1 predecessor had three fatal skews between training and live
(round-vs-floor join, dead one-hot vocabulary, self-feedback). That's why the
MIS1 pattern applies here (commit e84bc7d): tools/aim2_build_dataset.py and
15_ai_master_bot.py build each feature row using exactly this one function.

Contract:
  * market_row  = indicator row of the last CLOSED 1h candle before the event
    (floor-1 join; the caller is responsible for `open_time < floor(event)`),
    close = close of that candle.
  * regime_row  = most recent regime_history row with ts <= event (UTC!), None ok.
  * swarm       = 5d-swarm context WITHOUT AIM1/AIM2 and WITHOUT the event itself.
  * source      = source signal identity; the one-hot vocabulary is created during
    training from the data (artifact feature list), NOT from this file.

Absolute price / scale features are deliberately forbidden (ticker leakage,
Report 13).
"""

from __future__ import annotations

import math

# Price-based indicators → distance to close in % (scale-free)
MARKET_PRICE_COLS = [
    "ema_9",
    "ema_21",
    "ema_50",
    "ema_200",
    "kama_21",
    "wma_21",
    "boll_upper_20",
    "boll_mid_20",
    "boll_lower_20",
    "donchian_upper_20",
    "donchian_lower_20",
    "donchian_mid_20",
    "support_price",
    "resistance_price",
    "trendline_price",
]

# Already scale-free indicators → unchanged
MARKET_ABS_COLS = [
    "rsi_6",
    "rsi_14",
    "tsi_25_13_13",
    "macd_dif_normal_12_26_9",
    "macd_dea_normal_12_26_9",
    "trendline_slope",
    "r_squared",
]

ATR_COLS = ["atr_14", "atr_21"]  # → % of close

TREND_VALUES = ["UP", "DOWN", "SIDEWAYS"]
REGIME_VALUES = ["TREND_UP", "TREND_DOWN", "CHOP", "HIGH_VOLA", "TRANSITION"]
ALT_VALUES = ["ALT_STRONG", "ALT_NEUTRAL", "ALT_WEAK"]

# Conv signals have no model confidence — mapping as per Bot 15 (AIM1 era),
# so source_conf remains comparable across the AIM1→AIM2 break.
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

# Trailing WR semantics (closed_ai_signals) — MUST be identical in trainer and
# serving: win = any target hit.
TRAIL_WIN_SQL = "(status ILIKE '%%TARGET%%' OR COALESCE(targets_hit, 0) >= 1)"
TRAIL_WINDOW_DAYS = 30


def _f(value, default: float = 0.0) -> float:
    """Robust float conversion; NaN/inf/None/error → default."""
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
    """An event row → flat feature dict (finite floats only).

    source keys: name, type ('ai'|'conv'), conf, trail_wr_30d, trail_n_30d,
    entry_drift_pct, direction ('LONG'|'SHORT').
    swarm keys: total_5d, long_5d, short_5d, latest_age_h,
    confl_same_dir_4h, distinct_src_same_dir_4h.
    """
    row: dict[str, float] = {}
    close_safe = _f(close)
    if close_safe <= 0:
        close_safe = 1.0

    # --- Market (floor-1 candle) ---
    for col in MARKET_PRICE_COLS:
        val = _f(market_row.get(col), default=float("nan"))
        row[f"{col}_dist_pct"] = (val - close_safe) / close_safe * 100.0 if math.isfinite(val) and val > 0 else 0.0
    for col in MARKET_ABS_COLS:
        row[col] = _f(market_row.get(col))
    for col in ATR_COLS:
        row[f"{col}_pct_close"] = _f(market_row.get(col)) / close_safe * 100.0

    trend = str(market_row.get("trend_direction") or "nan")
    for t in TREND_VALUES:
        row[f"trend_{t}"] = 1.0 if trend == t else 0.0

    # --- Regime (the missing 2025 predictor) ---
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
    # Staleness capped: >6h means "regime info practically missing"
    row["regime_age_min"] = min(_f(regime_age_min, default=360.0), 360.0)

    # --- Swarm (without AIM1/AIM2, without the event itself — F6 fix) ---
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

    # --- Source ---
    name = str(source.get("name") or "nan").strip()
    row[f"src_{name}"] = 1.0  # Vocabulary is created during training (artifact list)
    row["src_is_ai"] = 1.0 if source.get("type") == "ai" else 0.0
    row["src_conf"] = _f(source.get("conf"))
    row["src_trail_wr_30d"] = _f(source.get("trail_wr_30d"), default=0.5)
    row["src_trail_n_30d"] = math.log1p(max(_f(source.get("trail_n_30d")), 0.0))
    row["entry_drift_pct"] = _f(source.get("entry_drift_pct"))
    row["direction_num"] = 1.0 if str(source.get("direction", "")).upper() == "LONG" else 0.0

    return row


def parity_nonzero_share(vector, feature_names) -> float:
    """Share of non-zero features — OOD guard against the P0.13 failure mode
    (serving reindex silently nulls half the vocabulary)."""
    nonzero = sum(1 for v in vector if _f(v) != 0.0)
    return nonzero / max(len(feature_names), 1)
