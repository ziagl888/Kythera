# core/breadth_features.py
"""Shared market breadth/dispersion feature builder (X-R1) — ONE source for
study, trainer and (later) bot/orchestrator.

Origin: docs/MODEL_CANDIDATES_SPEC_2026-07.md §K6 (BRD). The hypothesis: breadth
metrics across the ~530-strong USDT perp universe (share of coins > EMA200/EMA50,
median 7d return, advance/decline, return dispersion vs. BTC) complement the
BTC-only regime classification and potentially deliver the missing regime gate
for RUB-LONG. Like core/funding_features.py and core/aim2_features.py, this
builder is canonical: study, trainer and bot compute exactly the same metrics (no
train/serve skew).

Data source offline: the per-coin ``{SYM}_1d`` candles + ``{SYM}_1d_indicators``
(EMA50/EMA200 live there) via core.candles.read_candles_with_indicators.

Efficiency (mandatory, §K6): ONE query per coin (``load_universe_panels``), after
which the entire cross-section scaffold is built ONCE in-memory
(``build_breadth_panel``); the as-of evaluation (``breadth_features_asof``) is
then an O(log n) lookup into this precomputed daily panel — it does NOT
query 530 tables individually per point in time. The caller sets process priority
BELOW_NORMAL (tools/walkforward_sim.set_low_priority).

As-of contract (R1, closed candles only): a daily bar with open_time D
only closes at D + 1d. ``breadth_features_asof(panel, ts)`` therefore returns the
most recent daily bar D with D + tf <= ts — no lookahead. The load uses
``include_forming=False``.

Feature contract (X-R1): missing COLUMNS in the loaded frame ⇒ ``BreadthFeatureError``
(load error), NEVER ``fillna(0)`` as a contract substitute. Missing VALUES (coin
with too short a history, delisted coin without a table) are NOT a contract
breach: the coin simply drops out of the cross-section at that point in time
(exclusion, not zero). The contributing coin count is carried in every row as
the diagnostic metric ``brd_n_universe``.

TOTAL3 proxy — HONESTY NOTE (§K6 addendum): we have NO real market-cap
weights. The price index built here over the universe WITHOUT BTC/ETH is a
PROXY for the real TOTAL3 index (altcoin market cap excluding BTC/ETH). Two
variants:
  * equal-weighted (EW): every alt coin contributes its daily return equally.
  * volume-weighted (VW): weight = daily turnover proxy (close·volume, USD-close
    but base volume·price, not a real quote-volume column).
Both indices are return-chained levels (base 100), NOT a real market-cap
index. The practitioner gate idea "only trade alts while TOTAL3 is above a level"
(KB ingest-c1e5112dea7f) is thereby testable as a proxy, but must be documented
as a proxy — never presented as the real TOTAL3.

Consumer note: the raw ``total3_*_level`` is non-NaN from row 0 (the
base-100 anchor is already in place before the universe is fully populated) and
its absolute level is an arbitrary anchor with no cross-coin comparability. For
gates/features, the scale-free derivatives ``total3_*_dist_reg90d`` (distance
to the 90d regression) and ``total3_*_breakout`` (90d high) are preferable; the
level primarily serves their computation, not a direct comparison.

Survivorship (rule 9): coins.json lists the ACTIVE USDT perps; delisted coins
are partly missing. Every breadth row is therefore computed over a
survivorship-biased universe — a known, documented source of bias.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from core.candles import TF_SECONDS, read_candles_with_indicators


class BreadthFeatureError(RuntimeError):
    """X-R1 contract breach: a loaded frame does not carry the required columns."""


#: Columns that every loaded panel frame MUST carry (otherwise load error).
REQUIRED_COLUMNS: tuple[str, ...] = ("open_time", "close", "volume", "ema_50", "ema_200")

#: Indicator columns pulled from ``{SYM}_{tf}_indicators``.
PANEL_INDICATOR_COLS: tuple[str, ...] = ("ema_50", "ema_200")

#: The breadth/dispersion feature contract (canonical names, order fixed).
BREADTH_FEATURES: list[str] = [
    "brd_pct_above_ema200",  # share of coins with close > EMA200 (as-of)
    "brd_pct_above_ema50",  # share of coins with close > EMA50 (as-of)
    "brd_median_ret_7d",  # median 7d return across the universe
    "brd_adv_decline_ratio",  # advancer/decliner of the most recent daily bar
    "brd_dispersion_vs_btc",  # cross-section stddev of (7d return − BTC 7d return)
    "total3_ew_level",  # EW price index (proxy) level, base 100
    "total3_ew_dist_reg90d",  # distance to the 90d regression line (EW), relative
    "total3_ew_breakout",  # EW level > 90d prior high (1/0)
    "total3_vw_level",  # VW price index (proxy) level, base 100
    "total3_vw_dist_reg90d",  # distance to the 90d regression line (VW), relative
    "total3_vw_breakout",  # VW level > 90d prior high (1/0)
]

#: Diagnostic metric (not part of the feature contract), carried per row.
DIAGNOSTIC_COLUMNS: list[str] = ["brd_n_universe"]

RET_LOOKBACK_BARS = 7  # 7 daily bars → 7d return
REG_WINDOW_BARS = 90  # 90 daily bars → 90d regression / breakout window
INDEX_BASE = 100.0
BTC_SYMBOL = "BTCUSDT"
#: EXCLUDED from the TOTAL3 proxy (definition of the real TOTAL3).
EXCLUDED_FROM_TOTAL3: frozenset[str] = frozenset({"BTCUSDT", "ETHUSDT"})


def load_universe_panels(
    conn: Any,
    symbols: list[str],
    *,
    tf: str = "1d",
    start: Any | None = None,
) -> dict[str, pd.DataFrame]:
    """Loads ONE closed candle+indicator history per coin (ascending).

    One query per coin (read_candles_with_indicators, include_forming=False). Coins
    without a table/data (delisted, survivorship) are skipped — that is NOT a
    contract breach. If a LOADED frame is missing a required column, that is an
    X-R1 load error (BreadthFeatureError), never fillna(0).

    Returns: {symbol -> DataFrame[open_time(UTC, tz-aware), close, volume, ema_50,
    ema_200]}, ascending by open_time.
    """
    panels: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            df = read_candles_with_indicators(
                conn,
                sym,
                tf,
                start=start,
                include_forming=False,
                candle_columns=("open_time", "close", "volume"),
                indicator_columns=list(PANEL_INDICATOR_COLS),
            )
        except Exception:
            # Missing per-coin table or similar → survivorship, skip.
            conn.rollback()
            continue
        if df.empty:
            continue
        missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            raise BreadthFeatureError(f"{sym}_{tf}: missing required columns {missing} — X-R1 contract, no fillna(0)")
        df = df.copy()
        df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
        for col in ("close", "volume", "ema_50", "ema_200"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"]).sort_values("open_time").reset_index(drop=True)
        if df.empty:
            continue
        panels[sym] = df[["open_time", "close", "volume", "ema_50", "ema_200"]]
    if not panels:
        raise BreadthFeatureError("no usable panels loaded (universe empty?)")
    return panels


def _wide_field(panels: dict[str, pd.DataFrame], field: str) -> pd.DataFrame:
    """Builds a (date × symbol) matrix of one field; union index, NaN where missing."""
    series = {sym: df.set_index("open_time")[field] for sym, df in panels.items()}
    wide = pd.DataFrame(series).sort_index()
    # Duplicate open_times per coin (should not happen) → last wins.
    return wide[~wide.index.duplicated(keep="last")]


def _rolling_reg_distance(level: pd.Series, window: int) -> pd.Series:
    """Relative distance of the level to its own rolling OLS line.

    For every position i (from window-1) OLS of level[i-window+1 : i+1] against
    x = 0..window-1, prediction at the right edge, dist = (level - pred) / pred.
    NaN as long as the window is not full / contains a NaN.
    """
    vals = level.to_numpy(dtype=float)
    n = len(vals)
    out = np.full(n, np.nan)
    if n < window:
        return pd.Series(out, index=level.index)
    x = np.arange(window, dtype=float)
    xm = x.mean()
    xd = x - xm
    denom = float((xd * xd).sum())
    x_last = x[-1]
    for i in range(window - 1, n):
        y = vals[i - window + 1 : i + 1]
        if np.isnan(y).any():
            continue
        ym = y.mean()
        slope = float((xd * (y - ym)).sum()) / denom
        intercept = ym - slope * xm
        pred = slope * x_last + intercept
        if pred != 0:
            out[i] = (vals[i] - pred) / pred
    return pd.Series(out, index=level.index)


def _rolling_breakout(level: pd.Series, window: int) -> pd.Series:
    """1.0 if the level exceeds the high of the PRIOR ``window`` bars."""
    prior_max = level.shift(1).rolling(window).max()
    flag = (level > prior_max).astype(float)
    return flag.where(prior_max.notna())


def _index_levels(
    daily_ret: pd.DataFrame,
    turnover: pd.DataFrame,
    alt_cols: list[str],
) -> tuple[pd.Series, pd.Series]:
    """EW and VW price index (proxy, base 100) over the alt coins.

    Return-chained: a day without data contributes flat (return 0) — that is
    index construction, not feature fillna. The VW weights come from the
    turnover proxy (close·volume) of the same day.
    """
    if not alt_cols:
        empty = pd.Series(np.nan, index=daily_ret.index)
        return empty, empty
    alt_ret = daily_ret[alt_cols]
    ew_daily = alt_ret.mean(axis=1, skipna=True)
    ew_level = (1.0 + ew_daily.fillna(0.0)).cumprod() * INDEX_BASE

    turn = turnover[alt_cols]
    weights = turn.div(turn.sum(axis=1), axis=0)
    vw_daily = (alt_ret * weights).sum(axis=1, min_count=1)
    vw_level = (1.0 + vw_daily.fillna(0.0)).cumprod() * INDEX_BASE
    return ew_level, vw_level


def build_breadth_panel(panels: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Builds the daily cross-section panel of all breadth/dispersion features ONCE.

    Index = daily open_time (UTC, tz-aware). Columns = BREADTH_FEATURES +
    DIAGNOSTIC_COLUMNS. After this, ``breadth_features_asof`` is just a lookup.
    """
    close_wide = _wide_field(panels, "close")
    ema200_wide = _wide_field(panels, "ema_200")
    ema50_wide = _wide_field(panels, "ema_50")
    vol_wide = _wide_field(panels, "volume")
    turnover = close_wide * vol_wide

    # % above EMA200 / EMA50 (only coins with both values count).
    valid200 = close_wide.notna() & ema200_wide.notna() & (ema200_wide > 0)
    valid50 = close_wide.notna() & ema50_wide.notna() & (ema50_wide > 0)
    above200 = (close_wide > ema200_wide) & valid200
    above50 = (close_wide > ema50_wide) & valid50
    n200 = valid200.sum(axis=1)
    n50 = valid50.sum(axis=1)
    pct_above_ema200 = above200.sum(axis=1) / n200.where(n200 > 0)
    pct_above_ema50 = above50.sum(axis=1) / n50.where(n50 > 0)

    # fill_method=None: gaps stay NaN. The pandas default (pad) would forward-fill
    # a delisted coin's trailing NaNs into fabricated 0.0 daily returns for every
    # day after it stopped trading, diluting the equal-weighted TOTAL3 index toward
    # flat — a silent survivorship bias. With None a delisted coin drops out cleanly
    # (ret7d, the / shift form, is already unpadded). The VW path is likewise clean:
    # its weights come from unpadded turnover → NaN weight → excluded.
    daily_ret = close_wide.pct_change(fill_method=None)
    ret7d = close_wide / close_wide.shift(RET_LOOKBACK_BARS) - 1.0
    median_ret_7d = ret7d.median(axis=1, skipna=True)

    adv = (daily_ret > 0).sum(axis=1)
    dec = (daily_ret < 0).sum(axis=1)
    adv_decline_ratio = adv / dec.where(dec > 0)

    if BTC_SYMBOL in ret7d.columns:
        # Cross-section dispersion of the universe's 7d returns relative to the BTC
        # benchmark. BTC is the reference, so its own column would be a constant 0 —
        # excluded from the std columns so it does not damp the dispersion toward
        # zero. ETH stays in (it is a genuine universe member, not the benchmark).
        btc_ret7d = ret7d[BTC_SYMBOL]
        non_btc = [c for c in ret7d.columns if c != BTC_SYMBOL]
        rel = ret7d[non_btc].sub(btc_ret7d, axis=0)
        dispersion_vs_btc = rel.std(axis=1, skipna=True)
    else:
        dispersion_vs_btc = pd.Series(np.nan, index=close_wide.index)

    alt_cols = [c for c in close_wide.columns if c not in EXCLUDED_FROM_TOTAL3]
    ew_level, vw_level = _index_levels(daily_ret, turnover, alt_cols)

    n_universe = close_wide.notna().sum(axis=1)

    panel = pd.DataFrame(
        {
            "brd_pct_above_ema200": pct_above_ema200,
            "brd_pct_above_ema50": pct_above_ema50,
            "brd_median_ret_7d": median_ret_7d,
            "brd_adv_decline_ratio": adv_decline_ratio,
            "brd_dispersion_vs_btc": dispersion_vs_btc,
            "total3_ew_level": ew_level,
            "total3_ew_dist_reg90d": _rolling_reg_distance(ew_level, REG_WINDOW_BARS),
            "total3_ew_breakout": _rolling_breakout(ew_level, REG_WINDOW_BARS),
            "total3_vw_level": vw_level,
            "total3_vw_dist_reg90d": _rolling_reg_distance(vw_level, REG_WINDOW_BARS),
            "total3_vw_breakout": _rolling_breakout(vw_level, REG_WINDOW_BARS),
            "brd_n_universe": n_universe.astype(float),
        },
        index=close_wide.index,
    )
    return panel.sort_index()


def breadth_features_asof(panel: pd.DataFrame, ts_utc: Any, *, tf: str = "1d") -> dict:
    """The breadth features as-of ``ts_utc`` (tz-aware or naive=UTC).

    Returns the most recent daily bar D with D + tf <= ts (closed candles only,
    no lookahead). Returns {} for an empty panel or if ts lies before the first
    usable row. Values that are NaN come back as ``None`` (the caller decides —
    trainer discards the row; a gate fails closed/open) —
    NEVER as 0.
    """
    if panel.empty:
        return {}
    ts = pd.Timestamp(ts_utc)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    cutoff = ts - pd.Timedelta(seconds=TF_SECONDS[tf])
    idx = int(panel.index.searchsorted(cutoff, side="right")) - 1
    if idx < 0:
        return {}
    row = panel.iloc[idx]
    return {col: (float(row[col]) if pd.notna(row[col]) else None) for col in panel.columns}
