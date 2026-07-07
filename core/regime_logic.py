# core/regime_logic.py
"""
Shared regime classification logic used by:
  - 26_regime_detector.py  (live mode, as_of=None)
  - backtest/backfill_regime_history.py  (historical mode, as_of=<datetime>)

Implements the two-axis classification:
  Axis 1: BTC-Regime (TREND_UP, TREND_DOWN, CHOP, HIGH_VOLA, TRANSITION)
  Axis 2: Alt-Context (ALT_STRONG, ALT_NEUTRAL, ALT_WEAK) based on BTCDOM

Python files with numeric prefixes (26_...) cannot be imported directly.
This module is the importable entry point.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ── Thresholds ──────────────────────────────────────────────────────────────
TREND_RETURN_THRESHOLD_4H_PCT = 1.5  # > ±1.5% in 4h = Trend-Indikation
CHOP_RETURN_THRESHOLD_4H_PCT = 0.5  # < ±0.5% in 4h = Chop-Indikation
VOLA_HIGH_PERCENTILE = 75  # ATR über P75 der letzten 30d = HIGH_VOLA
VOLA_LOW_PERCENTILE = 40  # ATR unter P40 = niedrige Vola
VOLA_LOOKBACK_DAYS = 30
ALT_CONTEXT_THRESHOLD_PCT = 1.5  # |BTCDOM 24h| > 1.5% → ALT_STRONG/ALT_WEAK
REGIME_DEBOUNCE_COUNT = 2  # 2 Checks = 10 Minuten Bestätigung
MIN_DATA_POINTS_15M = 480  # 480 × 15min = 5 Tage minimum

# Mid-Vola-Trend-Regel (MODEL_INTENT §22, Operator-Pick 2026-07-07 nach
# tools/regime_rules_study.py — Variante V2_atr_1.5): Das Band P40..P75 war
# vorher TRANSITION-Restklasse (41 % der Zeit), TREND kam praktisch nie vor
# (3 Episoden in 430 Tagen, alle <1h). Vol-skalierte Regel: ein 4h-Return,
# der das Mehrfache der eigenen 4h-ATR schafft, IST ein Trend — unabhängig
# vom absoluten Vola-Niveau. Studie: RUB-LONG in TREND_UP +1,42 %/Trade
# (n=1.077) vs. −0,31 % gesamt.
MID_TREND_ATR_ENTER = 1.5  # Einstieg: |ret_4h| ≥ 1,5 × ATR_4h%
MID_TREND_ATR_EXIT = 1.0   # Hysterese: bestehender TREND hält bis |ret_4h| < 1,0 × ATR
TREND_DEBOUNCE_COUNT = 3   # TREND braucht 3 Checks (15 min) statt 2 — Flap-Dämpfung
                           # (Studie: 34 % der TREND-Episoden <1h ohne Zusatzdämpfung)


# ── Feature computation ────────────────────────────────────────────────────────


def compute_features(conn, as_of: datetime | None = None) -> dict | None:
    """
    Loads BTC + BTCDOM prices from 15m tables and computes regime features.

    Args:
        conn:   DB connection (pooled).
        as_of:  If None, uses current time (live mode).
                If a datetime, computes features as they would have been
                at that historical point in time (backfill mode).

    Returns dict with keys:
        btc_price, btc_return_1h, btc_return_4h,
        btc_atr_1h_pct, btc_atr_4h_pct,
        btcdom_value, btcdom_return_24h (may be None),
        vola_p75, vola_p40
    Or None if data is insufficient.
    """
    if as_of is None:
        as_of = datetime.now(timezone.utc)

    # Naive UTC for SQL (the tables store naive timestamps)
    if as_of.tzinfo is not None:
        as_of_naive = as_of.replace(tzinfo=None)
    else:
        as_of_naive = as_of

    lookback_start = as_of_naive - pd.Timedelta(days=VOLA_LOOKBACK_DAYS + 1)

    # ── BTC data ──
    try:
        df_btc = pd.read_sql_query(
            'SELECT open_time, high, low, close FROM "BTCUSDT_15m" '
            'WHERE open_time >= %s AND open_time <= %s '
            'ORDER BY open_time ASC',
            conn,
            params=(lookback_start, as_of_naive),
        )
    except Exception as e:
        logger.error(f"Error loading von BTCUSDT_15m: {e}")
        return None

    if len(df_btc) < MIN_DATA_POINTS_15M:
        logger.warning(f"Insufficient BTC data: {len(df_btc)} < {MIN_DATA_POINTS_15M} Kerzen")
        return None

    df_btc = df_btc.set_index("open_time")

    close = df_btc["close"]
    high = df_btc["high"]
    low = df_btc["low"]

    # Returns: 4 rows = 1h, 16 rows = 4h
    btc_return_1h = float(close.pct_change(4).iloc[-1] * 100)
    btc_return_4h = float(close.pct_change(16).iloc[-1] * 100)

    # ATR as % of close (True Range)
    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    # ATR_1h (EMA over 4 bars), ATR_4h (EMA over 16 bars)
    atr_1h = tr.ewm(span=4, adjust=False).mean()
    atr_4h = tr.ewm(span=16, adjust=False).mean()
    btc_atr_1h_pct = float(atr_1h.iloc[-1] / close.iloc[-1] * 100)
    btc_atr_4h_pct = float(atr_4h.iloc[-1] / close.iloc[-1] * 100)

    # Vola percentiles over last VOLA_LOOKBACK_DAYS (in percent)
    vola_series = atr_4h / close * 100
    vola_p75 = float(np.nanpercentile(vola_series.dropna(), VOLA_HIGH_PERCENTILE))
    vola_p40 = float(np.nanpercentile(vola_series.dropna(), VOLA_LOW_PERCENTILE))

    btc_price = float(close.iloc[-1])

    # ── BTCDOM data (optional) ──
    btcdom_value: float | None = None
    btcdom_return_24h: float | None = None
    lookback_btcdom = as_of_naive - pd.Timedelta(days=2)

    try:
        df_dom = pd.read_sql_query(
            'SELECT open_time, close FROM "BTCDOMUSDT_15m" '
            'WHERE open_time >= %s AND open_time <= %s '
            'ORDER BY open_time ASC',
            conn,
            params=(lookback_btcdom, as_of_naive),
        )
        if len(df_dom) >= 96:  # 96 × 15min = 24h minimum
            dom_close = df_dom["close"]
            btcdom_value = float(dom_close.iloc[-1])
            btcdom_return_24h = float((dom_close.iloc[-1] - dom_close.iloc[-96]) / dom_close.iloc[-96] * 100)
        else:
            logger.warning(
                f"BTCDOMUSDT_15m only has {len(df_dom)} candles — Alt-Context using safe default ALT_NEUTRAL"
            )
    except Exception as e:
        logger.warning(f"BTCDOMUSDT_15m not available: {e} — Alt-Context: ALT_NEUTRAL")

    return {
        "btc_price": btc_price,
        "btc_return_1h": btc_return_1h,
        "btc_return_4h": btc_return_4h,
        "btc_atr_1h_pct": btc_atr_1h_pct,
        "btc_atr_4h_pct": btc_atr_4h_pct,
        "btcdom_value": btcdom_value,
        "btcdom_return_24h": btcdom_return_24h,
        "vola_p75": vola_p75,
        "vola_p40": vola_p40,
    }


# ── BTC-Regime-Classifier ─────────────────────────────────────────────────────


def classify_btc_regime(
    features: dict,
    vola_p75: float,
    vola_p40: float,
    prev_regime: str | None = None,
) -> tuple[str, float]:
    """
    Classifies the BTC regime from pure BTC features + vola percentiles.

    Priority order:
      1. Data quality check     → TRANSITION (conf=0.0)
      2. HIGH_VOLA              → ATR-4h > P75 (overrides everything)
      3. Clear trend            → low vola (ATR-4h < P40) AND significant return
      4. CHOP                   → low vola AND almost no return
      5. Mid-Vola-Trend (§22)   → P40..P75 AND |ret_4h| ≥ 1,5×ATR (Hysterese:
                                  bestehender TREND hält bis |ret_4h| < 1,0×ATR)
      6. Fallback               → TRANSITION (unklare Richtung)

    Args:
        prev_regime: aktuelles EFFEKTIVES Regime (regime_current) für die
                     Hysterese der Mid-Band-Regel; None ⇒ nur Enter-Schwelle
                     (Kaltstart/Backfill).

    Returns (regime_name, confidence 0.0-1.0).
    """
    btc_ret_4h = features["btc_return_4h"]
    btc_atr_4h = features["btc_atr_4h_pct"]

    if btc_atr_4h is None or btc_ret_4h is None:
        return ("TRANSITION", 0.0)

    # Rule 1: HIGH_VOLA — overrides trend logic
    if btc_atr_4h > vola_p75:
        excess = (btc_atr_4h - vola_p75) / max(vola_p75, 0.01)
        confidence = min(1.0, 0.5 + excess)
        return ("HIGH_VOLA", confidence)

    # Rule 2 & 3: Clear trend requires low volatility
    if btc_atr_4h < vola_p40:
        if btc_ret_4h > TREND_RETURN_THRESHOLD_4H_PCT:
            conf = min(1.0, btc_ret_4h / (TREND_RETURN_THRESHOLD_4H_PCT * 2))
            return ("TREND_UP", conf)
        if btc_ret_4h < -TREND_RETURN_THRESHOLD_4H_PCT:
            conf = min(1.0, abs(btc_ret_4h) / (TREND_RETURN_THRESHOLD_4H_PCT * 2))
            return ("TREND_DOWN", conf)
        if abs(btc_ret_4h) < CHOP_RETURN_THRESHOLD_4H_PCT:
            return ("CHOP", 0.8)

    # Rule 5 (NEU 2026-07-07, MODEL_INTENT §22): Mid-Vola-Band P40..P75 —
    # vol-skalierte Trend-Regel mit Hysterese statt TRANSITION-Restklasse.
    else:
        enter = MID_TREND_ATR_ENTER * btc_atr_4h
        hold = MID_TREND_ATR_EXIT * btc_atr_4h
        # Confidence-Skala analog Low-Vola-Zweig: 0,5 an der Enter-Schwelle,
        # 1,0 ab 2× Enter; im Hysterese-Halt entsprechend <0,5.
        conf = min(1.0, abs(btc_ret_4h) / (enter * 2))
        if btc_ret_4h >= enter or (prev_regime == "TREND_UP" and btc_ret_4h >= hold):
            return ("TREND_UP", conf)
        if btc_ret_4h <= -enter or (prev_regime == "TREND_DOWN" and btc_ret_4h <= -hold):
            return ("TREND_DOWN", conf)

    # Rule 6: Fallback — ambiguous direction
    return ("TRANSITION", 0.4)


# ── Alt-Context-Classifier ────────────────────────────────────────────────────


def classify_alt_context(features: dict) -> tuple[str, float]:
    """
    Classifies the alt-context from BTCDOM movement (24h change).

    Semantics:
      BTCDOM falls  (negative) → ALT_STRONG (capital rotates into alts)
      BTCDOM rises  (positive) → ALT_WEAK   (capital rotates back into BTC)
      BTCDOM stable            → ALT_NEUTRAL

    Returns (context_name, confidence 0.0-1.0).
    """
    btcdom_ret_24h = features.get("btcdom_return_24h")
    if btcdom_ret_24h is None:
        return ("ALT_NEUTRAL", 0.3)

    if btcdom_ret_24h < -ALT_CONTEXT_THRESHOLD_PCT:
        excess = abs(btcdom_ret_24h) / ALT_CONTEXT_THRESHOLD_PCT
        confidence = min(1.0, 0.5 + (excess - 1.0) * 0.5)
        return ("ALT_STRONG", confidence)

    if btcdom_ret_24h > ALT_CONTEXT_THRESHOLD_PCT:
        excess = btcdom_ret_24h / ALT_CONTEXT_THRESHOLD_PCT
        confidence = min(1.0, 0.5 + (excess - 1.0) * 0.5)
        return ("ALT_WEAK", confidence)

    # Neutral band — confidence highest at center
    neutrality = 1.0 - (abs(btcdom_ret_24h) / ALT_CONTEXT_THRESHOLD_PCT)
    confidence = 0.5 + 0.4 * neutrality  # 0.5 at edge, 0.9 at center
    return ("ALT_NEUTRAL", confidence)


# ── Combined Classifier ───────────────────────────────────────────────────────


def classify_regime(
    features: dict,
    vola_p75: float,
    vola_p40: float,
    prev_regime: str | None = None,
) -> dict:
    """
    Main entry point: classifies both axes and returns combined result.

    prev_regime: aktuelles effektives BTC-Regime für die Mid-Band-Hysterese
    (siehe classify_btc_regime); None ⇒ nur Enter-Schwelle.

    Returns:
        {
            'regime': str,          # BTC-Regime
            'alt_context': str,     # Alt-Context
            'confidence': float,    # Overall confidence (min of both)
            'confidence_btc': float,
            'confidence_alt': float,
        }
    """
    btc_regime, conf_btc = classify_btc_regime(features, vola_p75, vola_p40, prev_regime=prev_regime)
    alt_context, conf_alt = classify_alt_context(features)

    return {
        "regime": btc_regime,
        "alt_context": alt_context,
        "confidence": min(conf_btc, conf_alt),
        "confidence_btc": conf_btc,
        "confidence_alt": conf_alt,
    }


# ── Debounce ──────────────────────────────────────────────────────────────────


def apply_debounce(
    conn,
    raw_regime: str,
    raw_alt_context: str,
    raw_confidence: float,
    raw_ts: datetime,
) -> dict:
    """
    Reads regime_current, compares with raw values, manages debounce state
    for BOTH axes independently.

    The two axes are debounced independently — it's valid for only one axis to
    change while the other stays stable.

    Returns:
        {
            'effective_regime': str,
            'effective_alt_context': str,
            'btc_regime_changed': bool,
            'alt_context_changed': bool,
        }
    """
    raw_ts_naive = raw_ts.replace(tzinfo=None) if raw_ts.tzinfo else raw_ts

    with conn.cursor() as cur:
        cur.execute(
            "SELECT regime, alt_context, since, alt_context_since, "
            "pending_regime, pending_count, pending_alt_context, pending_alt_count "
            "FROM regime_current WHERE id = 1"
        )
        row = cur.fetchone()

    # ── Cold start: initialize regime_current ────────────────────────────────
    if row is None:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO regime_current
                    (id, regime, alt_context, since, alt_context_since,
                     confidence, last_raw_regime, last_raw_alt_context, last_raw_ts,
                     pending_regime, pending_count,
                     pending_alt_context, pending_alt_count)
                VALUES (1, %s, %s, %s, %s, %s, %s, %s, %s, NULL, 0, NULL, 0)
                """,
                (
                    raw_regime,
                    raw_alt_context,
                    raw_ts_naive,
                    raw_ts_naive,
                    raw_confidence,
                    raw_regime,
                    raw_alt_context,
                    raw_ts_naive,
                ),
            )
        conn.commit()
        logger.info(f"🆕 Regime initialised: {raw_regime} / {raw_alt_context} (conf {raw_confidence:.2f})")
        return {
            "effective_regime": raw_regime,
            "effective_alt_context": raw_alt_context,
            "btc_regime_changed": False,
            "alt_context_changed": False,
        }

    (cur_regime, cur_alt, cur_since, cur_alt_since, pend_regime, pend_count, pend_alt, pend_alt_count) = row

    btc_changed = False
    alt_changed = False
    new_regime = cur_regime
    new_alt = cur_alt
    new_since = cur_since
    new_alt_since = cur_alt_since
    new_pend_regime = pend_regime
    new_pend_count = pend_count
    new_pend_alt = pend_alt
    new_pend_alt_count = pend_alt_count

    # ── BTC-Regime debounce ───────────────────────────────────────────────────
    # TREND-Ziele brauchen TREND_DEBOUNCE_COUNT Checks (Flap-Dämpfung, §22);
    # alle anderen Ziel-Regime wie bisher REGIME_DEBOUNCE_COUNT.
    needed = TREND_DEBOUNCE_COUNT if str(raw_regime).startswith("TREND") else REGIME_DEBOUNCE_COUNT
    if raw_regime == cur_regime:
        # Stable — reset pending
        new_pend_regime = None
        new_pend_count = 0
    else:
        if pend_regime == raw_regime:
            # Consecutive check with same new value → count towards confirm
            new_pend_count = pend_count + 1
            if new_pend_count >= needed:
                logger.info(f"🔄 BTC-Regime confirmed: {cur_regime} → {raw_regime} (after {new_pend_count} checks)")
                new_regime = raw_regime
                new_since = raw_ts_naive
                new_pend_regime = None
                new_pend_count = 0
                btc_changed = True
        else:
            # Different pending value — start fresh
            new_pend_regime = raw_regime
            new_pend_count = 1

    # ── Alt-Context debounce ──────────────────────────────────────────────────
    if raw_alt_context == cur_alt:
        new_pend_alt = None
        new_pend_alt_count = 0
    else:
        if pend_alt == raw_alt_context:
            new_pend_alt_count = pend_alt_count + 1
            if new_pend_alt_count >= REGIME_DEBOUNCE_COUNT:
                logger.info(
                    f"🔄 Alt-Context confirmed: {cur_alt} → {raw_alt_context} (after {new_pend_alt_count} checks)"
                )
                new_alt = raw_alt_context
                new_alt_since = raw_ts_naive
                new_pend_alt = None
                new_pend_alt_count = 0
                alt_changed = True
        else:
            new_pend_alt = raw_alt_context
            new_pend_alt_count = 1

    # ── Persist updated state ─────────────────────────────────────────────────
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE regime_current SET
                regime = %s,
                alt_context = %s,
                since = %s,
                alt_context_since = %s,
                confidence = %s,
                last_raw_regime = %s,
                last_raw_alt_context = %s,
                last_raw_ts = %s,
                pending_regime = %s,
                pending_count = %s,
                pending_alt_context = %s,
                pending_alt_count = %s
            WHERE id = 1
            """,
            (
                new_regime,
                new_alt,
                new_since,
                new_alt_since,
                raw_confidence,
                raw_regime,
                raw_alt_context,
                raw_ts_naive,
                new_pend_regime,
                new_pend_count,
                new_pend_alt,
                new_pend_alt_count,
            ),
        )
    conn.commit()

    return {
        "effective_regime": new_regime,
        "effective_alt_context": new_alt,
        "btc_regime_changed": btc_changed,
        "alt_context_changed": alt_changed,
    }
