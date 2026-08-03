# core/research_features.py — shared feature builders for research bots 30–33
# (PEX1, FMR1, TRM1, FIF1 — Report 15: S6, S8, S10, S11).
#
# ONE source for bot, dataset builder and trainer (X-R-Fix "trainer imports
# the bot's feature builder", cf. core/mis_features.py / core/aim2_features.py).
# Each feature is scale-free (%, ratio, oscillator, flag) — price-scale columns
# deliberately don't go in here (Report-13 leakage class 13-P1).

from __future__ import annotations

import math

import numpy as np
import pandas as pd

from core.candles import read_candles_with_indicators

# ── Shared market context (1h candles + indicator join) ───────────────────
# SELECT fragment for the indicator join (h = candle table, i = indicators).
CONTEXT_SQL_SELECT = """
    i.rsi_14, i.ema_21, i.ema_200, i.atr_14,
    i.boll_upper_20, i.boll_lower_20
"""

# Pure indicator column names from CONTEXT_SQL_SELECT (i. prefix + whitespace
# removed). ONE source for the live join (fetch_context_frame below) AND the
# offline join (tools/research_dataset_common.load_candles_ctx imports this list),
# so frame columns from serving and training/replay remain byte-identical (hard
# rule 7). read_candles_with_indicators expects the bare names.
CONTEXT_IND_COLS = [c.strip().split(".")[-1] for c in CONTEXT_SQL_SELECT.split(",") if c.strip()]

CONTEXT_FEATURES = [
    "ret_1h_pct",
    "ret_4h_pct",
    "ret_24h_pct",
    "atr_14_pct",
    "ctx_rsi_14",
    "vol_ratio_sma20",
    "dist_ema21_pct",
    "dist_ema200_pct",
    "boll_pos_20",
]

# Minimum window to make ret_24h + SMA20 computable.
CONTEXT_MIN_CANDLES = 30


def _safe_div(a: float, b: float, default: float = 0.0) -> float:
    if b is None or a is None or not np.isfinite(a) or not np.isfinite(b) or b == 0:
        return default
    return a / b


def candle_context_features(df: pd.DataFrame, idx: int) -> dict:
    """Scale-free market context of the candle at ``idx`` (last CLOSED candle).

    ``df``: chronologically ASC sorted, columns close, volume + CONTEXT_SQL_SELECT.
    The caller guarantees idx >= CONTEXT_MIN_CANDLES − 1 (else ValueError) —
    no silent fillna over a short window (P0.12 failure mode).
    """
    if idx < CONTEXT_MIN_CANDLES - 1:
        raise ValueError(f"Context window too short (idx={idx}, min={CONTEXT_MIN_CANDLES - 1})")

    close = float(df["close"].iloc[idx])
    if close <= 0:
        raise ValueError("close <= 0 — context not computable")

    def ret_pct(back: int) -> float:
        prev = float(df["close"].iloc[idx - back])
        return _safe_div(close - prev, prev) * 100.0

    vol = float(df["volume"].iloc[idx])
    vol_sma20 = float(df["volume"].iloc[idx - 19 : idx + 1].mean())

    def num(col: str, default: float) -> float:
        # NaN is truthy — `to_numeric(...) or default` would never hit the
        # default and would overwrite legitimate 0.0 values (review fix
        # 2026-07-06).
        v = pd.to_numeric(df[col].iloc[idx], errors="coerce")
        return default if pd.isna(v) else float(v)

    atr = num("atr_14", 0.0)
    rsi = num("rsi_14", 50.0)
    ema21 = num("ema_21", 0.0)
    ema200 = num("ema_200", 0.0)
    b_up = num("boll_upper_20", 0.0)
    b_lo = num("boll_lower_20", 0.0)

    feats = {
        "ret_1h_pct": ret_pct(1),
        "ret_4h_pct": ret_pct(4),
        "ret_24h_pct": ret_pct(24),
        "atr_14_pct": _safe_div(atr, close) * 100.0,
        "ctx_rsi_14": rsi,
        "vol_ratio_sma20": _safe_div(vol, vol_sma20, 1.0),
        "dist_ema21_pct": _safe_div(close - ema21, ema21) * 100.0,
        "dist_ema200_pct": _safe_div(close - ema200, ema200) * 100.0,
        "boll_pos_20": _safe_div(close - b_lo, b_up - b_lo, 0.5),
    }
    # Imputation identical bot == trainer (P2.34): inf → 0, NaN → 0.
    return {k: (float(v) if np.isfinite(v) else 0.0) for k, v in feats.items()}


# ── Regime context (regime_history / regime_current) ─────────────────────────
REGIME_CLASSES = ["TREND_UP", "TREND_DOWN", "CHOP", "HIGH_VOLA", "TRANSITION"]

REGIME_FEATURES = [f"regime_is_{r}" for r in REGIME_CLASSES] + ["regime_conf", "regime_age_min"]


def regime_features(regime_row: dict | None, age_min: float) -> dict:
    """One-hot of regime + confidence + age. ``regime_row=None`` (no history
    available) → all hots 0, conf 0, age capped at 360 min."""
    out = {f"regime_is_{r}": 0.0 for r in REGIME_CLASSES}
    conf = 0.0
    if regime_row is not None:
        r = str(regime_row.get("regime", "")).upper()
        if f"regime_is_{r}" in out:
            out[f"regime_is_{r}"] = 1.0
        c = regime_row.get("confidence")
        conf = float(c) if c is not None and np.isfinite(float(c)) else 0.0
    out["regime_conf"] = conf
    out["regime_age_min"] = float(min(age_min, 360.0))
    return out


# ── S6 / PEX1 — pump-exhaustion short ────────────────────────────────────────
# Event features = exactly the 4 metrics that 10_pump_dump_detector.py writes to
# pump_dump_events (the table's indicator columns have been NULL since P1.40 —
# that's why the indicator context comes from the 1h join).
PEX1_EVENT_FEATURES = ["ev_volume_ratio", "ev_price_change_60s", "ev_buy_pressure", "ev_volatility"]

PEX1_FEATURES = PEX1_EVENT_FEATURES + CONTEXT_FEATURES

# Gate as in training AND in the EPD1 live path (Report 13 EPD1-P0): only events
# with volume_ratio >= 5 are in-distribution. Pumps = positive 60s change.
PEX1_MIN_VOL_RATIO = 5.0
PEX1_MIN_PUMP_PCHG_60S = 1.5


def build_pex1_row(event: dict, df: pd.DataFrame, idx: int) -> dict:
    feats = {
        "ev_volume_ratio": float(event["volume_ratio"]),
        "ev_price_change_60s": float(event["price_change_60s"]),
        "ev_buy_pressure": float(event["buy_pressure"]),
        "ev_volatility": float(event["volatility"]),
    }
    feats.update(candle_context_features(df, idx))
    return feats


# ── S8 / FMR1 — funding-extreme mean-reversion ───────────────────────────────
FMR1_FEATURES = [
    "funding_rate_bps",  # current rate in basis points (rate × 1e4)
    "funding_cs_pctl",  # cross-sectional percentile across all coins (0..1)
    "funding_z_30d",  # Z-score against own last 90 settlements
    "funding_delta_8h_bps",  # change vs previous settlement
    "funding_sum_3d_bps",  # cumulative rate of last 9 settlements (carry)
    "side_short",  # 1 = SHORT (top extreme), 0 = LONG (bottom extreme)
] + CONTEXT_FEATURES

# Cross-sectional extreme gates (Report 15 S8): top/bottom percentile.
FMR1_SHORT_PCTL = 0.95
FMR1_LONG_PCTL = 0.05
FMR1_HISTORY_SETTLEMENTS = 90  # 30 days × 3 settlements


def funding_stats(rates: list[float]) -> dict:
    """Statistical features from the settlement history of ONE symbol.

    ``rates``: chronologically ASC, last element = most recent rate.
    Requires >= 10 settlements, else ValueError (no silent default rates).
    """
    if len(rates) < 10:
        raise ValueError(f"Funding history too short ({len(rates)} settlements)")
    arr = np.asarray(rates, dtype=np.float64) * 1e4  # → bps
    cur = float(arr[-1])
    hist = arr[-FMR1_HISTORY_SETTLEMENTS:]
    std = float(hist.std())
    return {
        "funding_rate_bps": cur,
        "funding_z_30d": _safe_div(cur - float(hist.mean()), std),
        "funding_delta_8h_bps": cur - float(arr[-2]),
        "funding_sum_3d_bps": float(arr[-9:].sum()),
    }


def build_fmr1_row(stats: dict, cs_pctl: float, side: str, df: pd.DataFrame, idx: int) -> dict:
    feats = dict(stats)
    feats["funding_cs_pctl"] = float(cs_pctl)
    feats["side_short"] = 1.0 if side.upper() == "SHORT" else 0.0
    feats.update(candle_context_features(df, idx))
    return feats


# ── K4 / FMR2 — funding-extreme-MR with normalisation exit ──────────────────
# Binding source: docs/NEW_IDEAS_BOTS.md §"FMR2 — own exit path" (+
# docs/MODEL_CANDIDATES_SPEC_2026-07.md §K4). FMR1 labelled first-touch TP/SL —
# that did NOT test the S8 thesis (Report 15 sketched "hold until funding
# normalisation OR time-stop"). FMR2 labels exactly this exit.
#
# ONE source for builder AND (the operator-gated) bot-31 exit loop, X-R1.
# Entry feature contract unchanged from FMR1_FEATURES (only the label changes)
# — extreme selection and 6 funding metrics remain identical.
FMR2_MODEL_ID = "FMR2"
FMR2_FEATURES = FMR1_FEATURES  # identical entry feature contract; label = normalisation exit

# Normalisation thresholds (design docs/NEW_IDEAS_BOTS.md §FMR2 order-1).
# Both metrics are exactly the entry features (funding_cs_pctl from cross-section,
# funding_z_30d from funding_stats) — same calculation, just re-evaluated per
# settlement during the hold phase (as-of, no lookahead).
#   SHORT (top extreme opened): normalised once funding_cs_pctl drops BACK
#     BELOW 0.80 OR funding_z_30d drops BACK BELOW 1.0.
#   LONG (bottom extreme) symmetrically: funding_cs_pctl ABOVE 0.20 OR
#     funding_z_30d ABOVE −1.0.
FMR2_SHORT_EXIT_CS_PCTL = 0.80
FMR2_SHORT_EXIT_Z = 1.0
FMR2_LONG_EXIT_CS_PCTL = 0.20
FMR2_LONG_EXIT_Z = -1.0

#: Time-stop: after 9 settlements (8h grid) = 3 days, force close.
FMR2_TIME_STOP_SETTLEMENTS = 9
#: Hard catastrophe SL in % from entry (convention K1-grid / P2.27-cap).
#: Remains active as a safety net BELOW the normalisation exit.
FMR2_CATASTROPHE_SL_PCT = 15.0


def fmr2_funding_normalized(direction: str, funding_cs_pctl: float, funding_z_30d: float) -> bool:
    """True once the funding extreme counts as *normalised* (exit trigger).

    OR logic: once ONE of the two metrics leaves the extreme zone, the
    mean-reversion thesis is satisfied — more conservative, earlier exit (thesis is
    "the extreme is unwinding", not "both metrics return together").

    Native NaN semantics: if one metric is NaN (e.g. std==0 in Z-score or
    sparse cross-section), both comparisons are False → *not* normalised
    → keep holding until time-stop. Deliberately fail-safe: an indeterminate
    normalisation does not close the trade early.
    """
    if direction.upper() == "SHORT":
        return funding_cs_pctl < FMR2_SHORT_EXIT_CS_PCTL or funding_z_30d < FMR2_SHORT_EXIT_Z
    return funding_cs_pctl > FMR2_LONG_EXIT_CS_PCTL or funding_z_30d > FMR2_LONG_EXIT_Z


def fmr2_catastrophe_sl(direction: str, entry_price: float) -> float:
    """Hard catastrophe SL price: entry ∓ FMR2_CATASTROPHE_SL_PCT.

    LONG → below entry, SHORT → above entry. Touch-based (liquidation is
    touch-based) — checked in builder as first-touch on 1h candles.
    """
    frac = FMR2_CATASTROPHE_SL_PCT / 100.0
    if direction.upper() == "SHORT":
        return entry_price * (1.0 + frac)
    return entry_price * (1.0 - frac)


# ── S10 / TRM1 — transition-resolution ───────────────────────────────────────
# Window = the last TRM1_WINDOW_CHECKS regime_history rows (5-min grid)
# up to and including the event. All inputs are already scale-free.
TRM1_WINDOW_CHECKS = 12  # 1h history

TRM1_FEATURES = [
    "btc_return_1h",
    "btc_return_4h",
    "btc_atr_1h_pct",
    "btc_atr_4h_pct",
    "btcdom_return_24h",
    "confidence_btc",
    "confidence_alt",
    "minutes_in_transition",
    "frac_up_1h",
    "frac_down_1h",
    "frac_chop_1h",
    "frac_highvola_1h",
    "btc_ret4h_delta_1h",
    "btc_ret4h_mean_1h",
    "btc_atr4h_delta_1h",
]

# Class contract of TRM1 model (multi:softprob) — trainer AND bot read it from
# here: 0 = no tradeable resolution, 1 = LONG thesis, 2 = SHORT thesis.
TRM1_CLASS_OTHER = 0
TRM1_CLASS_UP = 1
TRM1_CLASS_DOWN = 2


def build_trm1_row(window_rows: list[dict], minutes_in_transition: float) -> dict:
    """``window_rows``: chronologically ASC, last row = current check.

    Requires at least 2 rows; fraction features calculate over the actually
    available window (gaps in the 5-min grid are possible live).
    """
    if len(window_rows) < 2:
        raise ValueError("TRM1 window requires >= 2 regime_history rows")
    window_rows = window_rows[-TRM1_WINDOW_CHECKS:]
    cur = window_rows[-1]

    def f(row: dict, key: str) -> float:
        v = row.get(key)
        try:
            v = float(v)  # type: ignore[arg-type]  # None/non-numerisch → TypeError/ValueError unten gefangen
        except (TypeError, ValueError):
            return 0.0
        return v if np.isfinite(v) else 0.0

    regimes = [str(r.get("regime", "")).upper() for r in window_rows]
    n = float(len(regimes))
    ret4h_series = [f(r, "btc_return_4h") for r in window_rows]
    atr4h_series = [f(r, "btc_atr_4h_pct") for r in window_rows]

    return {
        "btc_return_1h": f(cur, "btc_return_1h"),
        "btc_return_4h": f(cur, "btc_return_4h"),
        "btc_atr_1h_pct": f(cur, "btc_atr_1h_pct"),
        "btc_atr_4h_pct": f(cur, "btc_atr_4h_pct"),
        "btcdom_return_24h": f(cur, "btcdom_return_24h"),
        "confidence_btc": f(cur, "confidence_btc"),
        "confidence_alt": f(cur, "confidence_alt"),
        "minutes_in_transition": float(min(minutes_in_transition, 1440.0)),
        "frac_up_1h": regimes.count("TREND_UP") / n,
        "frac_down_1h": regimes.count("TREND_DOWN") / n,
        "frac_chop_1h": regimes.count("CHOP") / n,
        "frac_highvola_1h": regimes.count("HIGH_VOLA") / n,
        "btc_ret4h_delta_1h": ret4h_series[-1] - ret4h_series[0],
        "btc_ret4h_mean_1h": float(np.mean(ret4h_series)),
        "btc_atr4h_delta_1h": atr4h_series[-1] - atr4h_series[0],
    }


# ── S11 / FIF1 — FIFO filter (meta-classifier over fast-in-and-out signals) ──
FIF1_FEATURES = (
    ["side_short"] + CONTEXT_FEATURES + REGIME_FEATURES + ["fifo_same_dir_24h", "fifo_fleet_1h", "hod_sin", "hod_cos"]
)


def build_fif1_row(
    direction: str,
    df: pd.DataFrame,
    idx: int,
    regime_row: dict | None,
    regime_age_min: float,
    fifo_same_dir_24h: int,
    fifo_fleet_1h: int,
    ts,
) -> dict:
    """``ts``: signal timestamp (naive UTC) — only for time-of-day features."""
    ts = pd.Timestamp(ts)
    hod = ts.hour + ts.minute / 60.0
    feats = {
        "side_short": 1.0 if direction.upper() == "SHORT" else 0.0,
        "fifo_same_dir_24h": float(fifo_same_dir_24h),
        "fifo_fleet_1h": float(fifo_fleet_1h),
        "hod_sin": math.sin(2 * math.pi * hod / 24.0),
        "hod_cos": math.cos(2 * math.pi * hod / 24.0),
    }
    feats.update(candle_context_features(df, idx))
    feats.update(regime_features(regime_row, regime_age_min))
    return feats


# ── Shared context frame fetch (bots 30/31/33) ──────────────────────────────
# Mirror of the training gate MAX_JOIN_STALENESS_H (tools/research_dataset_common):
# a feature candle older than 3h relative to the decision timestamp would have
# been rejected by training — live it must not feed a signal (review fix
# 2026-07-06: previously the guard was missing, ingestion lag → signals on
# hour-old prices).
CONTEXT_MAX_STALENESS_H = 3


def fetch_context_frame(conn, symbol: str, lookback: int = 60, as_of=None):
    """Last 1h candles + context indicators (CONTEXT_SQL_SELECT join).

    ``as_of``: decision timestamp (naive UTC or aware; default = now).
    Feature candle is the last CLOSED candle BEFORE the as_of hour —
    exactly the floor-1 join of the dataset builders (training-serving parity, R1).
    Event bots (PEX1) pass the event time so an event processed across an hour
    boundary sees the same candle as in training.

    Returns ``(df ASC, idx of feature candle)`` or None if insufficient data
    or if the feature candle is older than CONTEXT_MAX_STALENESS_H.
    """
    import datetime as _dt

    # R1 (hard rule 7): identical read path as the offline join in
    # tools/research_dataset_common.load_candles_ctx — core.candles delivers
    # CLOSED candles (include_forming=False) already ASC sorted. The former
    # DESC-SQL + .iloc[::-1] reversal is gone (INVERSE trap: if the reversal
    # remained, the frame would be DESC again and searchsorted below would miss).
    df = read_candles_with_indicators(
        conn,
        symbol,
        "1h",
        limit=int(lookback),
        include_forming=False,
        candle_columns=("open_time", "close", "volume"),
        indicator_columns=CONTEXT_IND_COLS,
    )
    if len(df) < CONTEXT_MIN_CANDLES + 1:
        return None
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True).dt.tz_localize(None)
    for c in df.columns:
        if c != "open_time":
            df[c] = pd.to_numeric(df[c], errors="coerce")

    if as_of is None:
        as_of = _dt.datetime.now(_dt.timezone.utc)
    as_of = pd.Timestamp(as_of)
    if as_of.tzinfo is not None:
        as_of = as_of.tz_convert("UTC").tz_localize(None)
    cur_hour = as_of.floor("h")

    times = df["open_time"]
    idx = int(times.searchsorted(cur_hour, side="left")) - 1  # last candle BEFORE the as_of hour
    if idx < CONTEXT_MIN_CANDLES - 1:
        return None
    if (cur_hour - times.iloc[idx]) > pd.Timedelta(hours=CONTEXT_MAX_STALENESS_H):
        return None  # stale join — training would have rejected this event
    return df, idx


# ── Shared assertion (P0.12 pattern) ──────────────────────────────────────────
def assert_features_alive(
    rows: list[dict], feature_cols: list[str], binary_ok: set[str] | None = None, context: str = ""
) -> None:
    """No continuous feature is constant across a sample of feature dicts.
    ``binary_ok``: flags that may legitimately be constant."""
    if not rows:
        raise ValueError(f"Feature-Assertion{context}: empty sample")
    binary_ok = binary_ok or set()
    df = pd.DataFrame(rows)
    missing = [c for c in feature_cols if c not in df.columns]
    if missing:
        raise ValueError(f"Feature-Assertion{context}: missing columns: {missing}")
    continuous = [c for c in feature_cols if c not in binary_ok]
    constant = [c for c in continuous if df[c].nunique(dropna=False) <= 1]
    if constant:
        raise ValueError(f"Feature-Assertion{context}: constant features: {constant}")
