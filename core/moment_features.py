# core/moment_features.py
"""Shared Realized-Moments feature builder (X-R1) — ONE source for study,
trainer and (later) bot.

Origin: docs/MODEL_CANDIDATES_SPEC_2026-07.md §K7 (MOM/SKW1). The hypothesis:
the realized distribution moments of a coin's recent return history
(volatility, skew, kurtosis) carry information — in particular realized
**skew** predicts negatively (short candidate filter SKW1), and RV/kurtosis
feed upcoming retrains (ATS2, QM2, BR gate). Like core/funding_features.py and
core/breadth_features.py this builder is canonical: study, trainer and bot
compute exactly the same quantities (no train/serve skew).

⚠ TRAP (§K7, F6): This is REALIZED SKEW (third moment of the
return distribution), NOT a MAX/lottery feature. MAX-based shorts are
contraindicated in crypto (F6 — the MAX effect inverts). Deliberately NO
"maximum single return in the window" is built, but rather the standard moment
estimators (pandas rolling std/skew/kurt) over the return series.

Data source offline: the per-coin ``{SYM}_15m`` candles via core.candles.read_candles.
DELIBERATELY 15m, NOT 5m: 5m only has ~1 month retention, 15m reaches back ~1 year
(§K7). Rolling windows {24h, 7d} — at 15m that is 96 and 672 closed
bars respectively.

As-of contract (R1, closed candles only): a 15m bar with open_time D
only closes at D + 15m. ``moment_features_asof(panel, ts)`` therefore returns the
most recent bar D with D + tf <= ts — no lookahead. The load uses
``include_forming=False``.

Native-NaN policy (XGB pattern P1.20): missing VALUES stay NaN and are NEVER
replaced with 0. A coin with too-short history returns NaN at a given point in time
(or ``None`` from the as-of function) — the caller decides (trainer:
discard the row; a gate: fail-closed/-open per policy). ``fillna(0)`` would fake a
flat distribution (vol 0, skew 0) and poison the model.

Feature contract (X-R1): missing COLUMNS in the loaded frame ⇒ ``MomentFeatureError``
(load error), NEVER ``fillna(0)`` as a contract substitute. Missing VALUES are NOT a
contract breach — they stay NaN (see above).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from core.candles import TF_SECONDS, read_candles


class MomentFeatureError(RuntimeError):
    """X-R1 contract breach: a loaded frame is missing the required columns."""


#: Columns that every loaded candle frame MUST carry (otherwise load error).
REQUIRED_COLUMNS: tuple[str, ...] = ("open_time", "close")

#: Rolling windows (§K7). Values in seconds → bar count depends on tf (15m→96/672).
WINDOW_SECONDS: dict[str, int] = {"24h": 86400, "7d": 604800}

#: The Realized-Moments feature contract (canonical names, order fixed).
#: 3 moments × 2 windows = 6 features (parallel to the 6 funding features).
MOMENT_FEATURES: list[str] = [
    "mom_rv_24h",  # realized vol: std dev of the 15m log returns over trailing 24h
    "mom_rv_7d",  # ... over trailing 7d
    "mom_skew_24h",  # realized (sample) skew of the 15m log returns over 24h
    "mom_skew_7d",  # ... over 7d
    "mom_kurt_24h",  # realized excess kurtosis (Fisher) over 24h
    "mom_kurt_7d",  # ... over 7d
]

#: Default tf of the builder (§K7: 15m due to retention).
DEFAULT_TF = "15m"


def window_bars(tf: str = DEFAULT_TF) -> dict[str, int]:
    """Bar count per window for a given tf (24h/7d in bars).

    ``MomentFeatureError`` for an unknown tf or if a window is not a whole
    multiple of the bar duration (in that case the window length would be undefined).
    """
    if tf not in TF_SECONDS:
        raise MomentFeatureError(f"unknown tf {tf!r} — no TF_SECONDS entry")
    step = TF_SECONDS[tf]
    bars: dict[str, int] = {}
    for name, secs in WINDOW_SECONDS.items():
        if secs % step != 0:
            raise MomentFeatureError(f"window {name} ({secs}s) is not a multiple of the {tf} bar duration ({step}s)")
        bars[name] = secs // step
    return bars


def load_moment_candles(
    conn: Any,
    symbol: str,
    *,
    tf: str = DEFAULT_TF,
    start: Any | None = None,
) -> pd.DataFrame:
    """Loads ONE closed 15m candle history for a coin (ascending).

    One query per coin (read_candles, include_forming=False). If the LOADED
    frame is missing a required column, that is an X-R1 load error (MomentFeatureError),
    never fillna(0). An empty frame (no coin/no data) is returned
    unchanged — the caller skips the coin (survivorship).

    Returns: DataFrame[open_time(UTC, tz-aware), close], ascending.
    """
    df = read_candles(
        conn,
        symbol,
        tf,
        start=start,
        include_forming=False,
        columns=("open_time", "close"),
    )
    if df.empty:
        return df
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise MomentFeatureError(f"{symbol}_{tf}: missing required columns {missing} — X-R1 contract, no fillna(0)")
    df = df.copy()
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values("open_time").reset_index(drop=True)
    return df[["open_time", "close"]]


def build_moment_panel(df: pd.DataFrame, *, tf: str = DEFAULT_TF) -> pd.DataFrame:
    """Builds the per-coin moment panel ONCE (rolling over the 15m returns).

    Index = 15m open_time (UTC, tz-aware). Columns = MOMENT_FEATURES. After that
    ``moment_features_asof`` is just an O(log n) lookup.

    Returns = log returns ``ln(close_t / close_{t-1})``. For each window the
    standard deviation (mom_rv, ddof=1), the sample skew (pandas .skew(),
    Fisher-Pearson bias-corrected) and the excess kurtosis (pandas .kurt(), Fisher)
    are computed over the trailing returns. ``min_periods`` = full window width:
    as long as the window is not full, the value stays NaN (native-NaN policy —
    NO fillna).

    An empty/too-short frame returns an empty panel with the correct columns.
    """
    bars = window_bars(tf)
    cols = {feat: np.nan for feat in MOMENT_FEATURES}
    if df.empty:
        return pd.DataFrame(cols, index=pd.DatetimeIndex([], tz="UTC", name="open_time"))

    d = df.sort_values("open_time").reset_index(drop=True)
    idx = pd.DatetimeIndex(pd.to_datetime(d["open_time"], utc=True), name="open_time")
    # Log returns; the first row has no predecessor → NaN (stays NaN).
    ret = np.log(d["close"].to_numpy(dtype=float))
    ret = pd.Series(np.diff(ret, prepend=np.nan), index=idx)

    out: dict[str, pd.Series] = {}
    for win_name, n in bars.items():
        roll = ret.rolling(window=n, min_periods=n)
        out[f"mom_rv_{win_name}"] = roll.std(ddof=1)
        out[f"mom_skew_{win_name}"] = roll.skew()
        out[f"mom_kurt_{win_name}"] = roll.kurt()

    panel = pd.DataFrame({feat: out[feat] for feat in MOMENT_FEATURES}, index=idx)
    return panel.sort_index()


def moment_features_asof(panel: pd.DataFrame, ts_utc: Any, *, tf: str = DEFAULT_TF) -> dict:
    """The Realized-Moment features as-of ``ts_utc`` (tz-aware or naive=UTC).

    Returns the most recent 15m bar D with D + tf <= ts (closed candles only,
    no lookahead). Returns {} for an empty panel or if ts is before the first
    usable row. Values that are NaN come back as ``None`` (the
    caller decides — trainer discards the row; a gate fail-closed/-open)
    — NEVER as 0.
    """
    if panel.empty:
        return {}
    if tf not in TF_SECONDS:
        raise MomentFeatureError(f"unknown tf {tf!r} — no TF_SECONDS entry")
    ts = pd.Timestamp(ts_utc)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    cutoff = ts - pd.Timedelta(seconds=TF_SECONDS[tf])
    idx = int(panel.index.searchsorted(cutoff, side="right")) - 1
    if idx < 0:
        return {}
    row = panel.iloc[idx]
    return {col: (float(row[col]) if pd.notna(row[col]) else None) for col in panel.columns}


def build_symbol_moment_panels(
    conn: Any,
    symbols: list[str],
    *,
    tf: str = DEFAULT_TF,
    start: Any | None = None,
) -> dict[str, pd.DataFrame]:
    """Convenience for study/trainer: one query per coin → finished moment panel.

    Coins without a table/data (delisted, survivorship) are skipped — that is
    NOT a contract breach. If a LOADED frame is missing a required column, the
    MomentFeatureError propagates (X-R1 load error).

    Returns: {symbol -> moment panel}. Coins without a usable panel are absent.
    """
    panels: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            df = load_moment_candles(conn, sym, tf=tf, start=start)
        except MomentFeatureError:
            raise
        except Exception:
            # Missing per-coin table or similar → survivorship, skip.
            try:
                conn.rollback()
            except Exception:
                pass
            continue
        if df.empty:
            continue
        panel = build_moment_panel(df, tf=tf)
        if panel.empty:
            continue
        panels[sym] = panel
    return panels
