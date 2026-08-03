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

from core.candles import last_closed_open_time, read_candles

logger = logging.getLogger(__name__)

# ── Thresholds ──────────────────────────────────────────────────────────────
TREND_RETURN_THRESHOLD_4H_PCT = 1.5  # > ±1.5% in 4h = trend indication
CHOP_RETURN_THRESHOLD_4H_PCT = 0.5  # < ±0.5% in 4h = chop indication
VOLA_HIGH_PERCENTILE = 75  # ATR above P75 of the last 30d = HIGH_VOLA
VOLA_LOW_PERCENTILE = 40  # ATR below P40 = low vola
VOLA_LOOKBACK_DAYS = 30
ALT_CONTEXT_THRESHOLD_PCT = 1.5  # |BTCDOM 24h| > 1.5% → ALT_STRONG/ALT_WEAK
REGIME_DEBOUNCE_COUNT = 2  # 2 checks = 10 minutes confirmation
MIN_DATA_POINTS_15M = 480  # 480 × 15min = 5 days minimum

# Mid-vola trend rule (MODEL_INTENT §22, operator pick 2026-07-07 after
# tools/regime_rules_study.py — variant V2_atr_1.5): the band P40..P75 was
# previously a TRANSITION residual class (41 % of the time), TREND practically
# never occurred (3 episodes in 430 days, all <1h). Vol-scaled rule: a 4h return
# that achieves a multiple of its own 4h ATR IS a trend — independent
# of the absolute vola level. Study: RUB-LONG in TREND_UP +1.42 %/trade
# (n=1,077) vs. −0.31 % overall.
MID_TREND_ATR_ENTER = 1.5  # entry: |ret_4h| ≥ 1.5 × ATR_4h%
MID_TREND_ATR_EXIT = 1.0  # hysteresis: existing TREND holds until |ret_4h| < 1.0 × ATR
TREND_DEBOUNCE_COUNT = 3  # TREND needs 3 checks (15 min) instead of 2 — flap dampening
# (study: 34 % of TREND episodes <1h without extra dampening)


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
    live = as_of is None
    if as_of is None:
        as_of = datetime.now(timezone.utc)

    # Aware UTC for the closed-candle boundary; naive UTC for the window start
    # (the legacy per-coin tables store naive timestamps). For the real callers
    # as_of is always UTC-aware, so this is byte-equal to the old
    # `as_of.replace(tzinfo=None)` — the astimezone only hardens a non-UTC input.
    if as_of.tzinfo is not None:
        as_of_aware = as_of.astimezone(timezone.utc)
    else:
        as_of_aware = as_of.replace(tzinfo=timezone.utc)
    as_of_naive = as_of_aware.replace(tzinfo=None)

    lookback_start = as_of_naive - pd.Timedelta(days=VOLA_LOOKBACK_DAYS + 1)

    # R1 (Block 5): read only CLOSED 15m candles via core.candles.
    #   * Live (as_of=now): include_forming=False drops the forming candle by
    #     the DB-clock cutoff — one clock, the writer's; no upper bound needed.
    #   * Backfill (as_of=historical): the DB-now() cutoff can't express "closed
    #     AT as_of", so bound the read explicitly with end=last_closed_open_time
    #     ('15m', as_of) (API `end` is inclusive → the candle still forming at
    #     as_of is excluded). This removes the look-ahead the old `<= as_of`
    #     window carried and makes a regenerated regime_history closed-candle-
    #     correct (prerequisite for the TRM1 retrain follow-up).
    end_15m = None if live else last_closed_open_time("15m", as_of_aware).replace(tzinfo=None)

    # ── BTC data ──
    try:
        df_btc = read_candles(
            conn,
            "BTCUSDT",
            "15m",
            start=lookback_start,
            end=end_15m,
            include_forming=False,
            columns=("open_time", "high", "low", "close"),
        )
    except Exception as e:
        logger.error(f"Error loading from BTCUSDT_15m: {e}")
        return None

    if len(df_btc) < MIN_DATA_POINTS_15M:
        logger.warning(f"Insufficient BTC data: {len(df_btc)} < {MIN_DATA_POINTS_15M} candles")
        return None

    # core.candles hands back raw psycopg2 NUMERIC (Decimal); the old
    # pd.read_sql_query returned float. pct_change/ewm/nanpercentile below need
    # float — cast explicitly (Block 4 bot-22 Decimal trap).
    df_btc = df_btc.set_index("open_time")
    for _c in ("high", "low", "close"):
        df_btc[_c] = pd.to_numeric(df_btc[_c], errors="coerce")

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
        df_dom = read_candles(
            conn,
            "BTCDOMUSDT",
            "15m",
            start=lookback_btcdom,
            end=end_15m,
            include_forming=False,
            columns=("open_time", "close"),
        )
        if len(df_dom) >= 96:  # 96 × 15min = 24h minimum
            dom_close = pd.to_numeric(df_dom["close"], errors="coerce")  # Decimal → float (s.o.)
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


# ── BTC regime classifier ──────────────────────────────────────────────────


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
      5. Mid-vola trend (§22)   → P40..P75 AND |ret_4h| ≥ 1.5×ATR (hysteresis:
                                  existing TREND holds until |ret_4h| < 1.0×ATR)
      6. Fallback               → TRANSITION (unclear direction)

    Args:
        prev_regime: regime reference for the mid-band hysteresis — effective
                     regime OR pending TREND (hysteresis_prev_regime from
                     the regime_current state, so the hold threshold already
                     applies during the debounce confirmation); None ⇒ only
                     the enter threshold (cold start/backfill).

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

    # Rule 5 (NEW 2026-07-07, MODEL_INTENT §22): mid-vola band P40..P75 —
    # vol-scaled trend rule with hysteresis instead of TRANSITION residual class.
    else:
        enter = MID_TREND_ATR_ENTER * btc_atr_4h
        hold = MID_TREND_ATR_EXIT * btc_atr_4h
        # Confidence scale analogous to the low-vola branch: 0.5 at the enter
        # threshold, 1.0 from 2× enter; correspondingly <0.5 during the hysteresis hold.
        conf = min(1.0, abs(btc_ret_4h) / (enter * 2))
        if btc_ret_4h >= enter or (prev_regime == "TREND_UP" and btc_ret_4h >= hold):
            return ("TREND_UP", conf)
        if btc_ret_4h <= -enter or (prev_regime == "TREND_DOWN" and btc_ret_4h <= -hold):
            return ("TREND_DOWN", conf)

    # Rule 6: Fallback — ambiguous direction
    return ("TRANSITION", 0.4)


# ── Alt-context classifier ───────────────────────────────────────────────────


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

    prev_regime: current effective BTC regime for the mid-band hysteresis
    (see classify_btc_regime); None ⇒ only the enter threshold.

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

#: Column order from read_regime_state — apply_debounce unpacks against it.
_STATE_COLUMNS = (
    "regime, alt_context, since, alt_context_since, "
    "pending_regime, pending_count, pending_alt_context, pending_alt_count"
)

#: Sentinel: state_row not passed in → apply_debounce reads it itself.
_STATE_UNREAD = object()


def read_regime_state(conn) -> tuple | None:
    """Reads the regime_current row ONCE per check (None = cold start).

    Callers pass the row on to hysteresis_prev_regime AND apply_debounce —
    one source per cycle instead of two separate reads of the same row.
    """
    with conn.cursor() as cur:
        cur.execute(f"SELECT {_STATE_COLUMNS} FROM regime_current WHERE id = 1")
        return cur.fetchone()


def hysteresis_prev_regime(state_row: tuple | None) -> str | None:
    """prev_regime for the §22 mid-band hysteresis from the debounce state.

    The hold threshold must also apply during the PENDING phase: TREND entry
    needs TREND_DEBOUNCE_COUNT consecutive raw checks, and as long as the
    effective regime is not yet TREND, a single dip below the enter
    threshold would reset the counter — TREND would then NEVER confirm on
    oscillation around the threshold (review finding PR #9). That is why a
    pending TREND counts like an existing one: enter once at 1.5×ATR, after
    that the hold threshold (1.0×ATR) is enough for the confirmation checks —
    this matches the §22 study semantics (enter once, then hold).

    Precedence: the EFFECTIVE TREND regime beats a pending TREND —
    otherwise a single counter-spike (raw TREND_DOWN pending while
    effectively TREND_UP) would strip the hold threshold from the LIVE
    trend and permanently flip it via the TRANSITION confirmation (verifier PR #10).
    """
    if state_row is None:
        return None
    regime, pending = state_row[0], state_row[4]
    if regime is not None and str(regime).startswith("TREND"):
        return str(regime)
    if pending is not None and str(pending).startswith("TREND"):
        return str(pending)
    return regime


def apply_debounce(
    conn,
    raw_regime: str,
    raw_alt_context: str,
    raw_confidence: float,
    raw_ts: datetime,
    state_row=_STATE_UNREAD,
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

    row = read_regime_state(conn) if state_row is _STATE_UNREAD else state_row

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

    # ── BTC regime debounce ───────────────────────────────────────────────────
    # TREND targets need TREND_DEBOUNCE_COUNT checks (flap dampening, §22);
    # all other target regimes as before REGIME_DEBOUNCE_COUNT.
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
