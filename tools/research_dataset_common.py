"""
tools/research_dataset_common.py — shared helpers for the research dataset builders
(pex1/fmr1/trm1/fif1_build_dataset.py). Patterns and TZ conventions from
tools/aim2_build_dataset.py; labels ALWAYS come from simulate_exit
(tools/walkforward_sim.py — wick-aware first-touch, SL-first, fees).
"""

from __future__ import annotations

import datetime as dt
import os
import sys

import numpy as np
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# cp1252 console: special characters in output must not abort the run
# (same fix as tools/retrain_from_replay.py, 13ce748).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from core.candles import read_candles_with_indicators  # noqa: E402

# CONTEXT_IND_COLS is the ONE source (core/research_features) from which the
# live join (fetch_context_frame) also draws its indicator columns — this keeps
# the frame columns of serving and training/replay byte-identical (hard rule 7).
from core.research_features import CONTEXT_IND_COLS  # noqa: E402
from core.time import LEGACY_WRITER_TZ, legacy_naive_to_utc, r3_history_mode  # noqa: E402

STAGING_DIR = os.getenv("KYTHERA_STAGING_DIR", r"C:\Users\Michael\Documents\_X\staging_models")
REPLAY_DIR = os.getenv("KYTHERA_REPLAY_DIR", os.path.join(STAGING_DIR, "replay"))

# The TZ in which the *_trades_master writers stamped BEFORE the R3 flip
# (T-2026-KYT-9050-005). No running writer uses it anymore — it is now only
# the reading of the history, and whether it is even needed is decided by
# core.time.R3_CUTOVER_UTC. Re-exported under the old name for the builders.
LOCAL_TZ = LEGACY_WRITER_TZ
MAX_JOIN_STALENESS_H = 3           # candle gap → discard event
MIN_WINDOW = 60                    # minimum candles before the event
WINDOW_CANDLES = 500               # smart-targets window


def log(msg: str) -> None:
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def set_low_priority() -> None:
    """VPS runs at the load limit — builders run with BELOW_NORMAL."""
    try:
        import psutil

        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass


def df_query(conn, sql: str, params=None) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
    return pd.DataFrame(rows, columns=cols)


_R3_MODE_LOGGED = False


def to_utc_naive(series: pd.Series) -> pd.Series:
    """Event times of a legacy column as naive UTC.

    Before the R3 flip (T-2026-KYT-9050-005) this hard-coded the PG local time
    out. The writers now stamp UTC; a fixed compensation would be a
    second shift. Whether and from when OLD rows still need to be localised
    depends on a single constant (core.time.R3_CUTOVER_UTC,
    docs/UTC_POLICY.md §6) — hence no more dedicated TZ logic here.

    Training-relevant: the reading is logged on the first call. A trainer
    that reads history under the wrong reading builds a train/serve skew (P0.13)
    — that must not happen silently."""
    global _R3_MODE_LOGGED
    if not _R3_MODE_LOGGED:
        _R3_MODE_LOGGED = True
        log(f"R3 time domain of the legacy columns: {r3_history_mode()} (docs/UTC_POLICY.md §6)")
    return legacy_naive_to_utc(series)


def candles_window_start(since: str, lookback_days: int):
    """Lower window bound as an aware datetime for core.candles.

    Reproduces the earlier ``%s::timestamptz - INTERVAL 'N days'`` of the builder
    SQL: ``since`` was interpreted DB-side in the session TZ (PG local == LOCAL_TZ)
    as a timestamptz. We localise identically and subtract the days.
    This is only a warmup lower bound well before the events — DST granularity
    (≤1h) is immaterial, and the Bucharest reading never cuts LATER than the
    old SQL, so it loses no candles.

    Deliberately NOT part of the R3 flip (T-2026-KYT-9050-005): here no
    column value is translated into its domain, but a calendar date into a
    deliberately conservative lower bound. Bucharest sits (2–3h) BEFORE the
    UTC reading, so the boundary stays valid under both regimes — at most it
    adds warmup candles, it never loses any.
    """
    ts = pd.Timestamp(since)
    if ts.tzinfo is None:
        ts = ts.tz_localize(LOCAL_TZ, nonexistent="shift_forward", ambiguous=True)
    return (ts - pd.Timedelta(days=int(lookback_days))).to_pydatetime()


def load_candles_ctx(conn, symbol: str, since: str, lookback_days: int = 30) -> pd.DataFrame | None:
    """1h candles + context indicators (CONTEXT_SQL_SELECT join), ASC, naive UTC.

    Via core.candles: CLOSED candles (include_forming=False). The callers
    already cut via floor_idx to the last closed candle before the
    event — they would never have picked the forming tail row; the switch is
    contract-compatible and removes a latent R1 repaint (report §3)."""
    try:
        df = read_candles_with_indicators(
            conn,
            symbol,
            "1h",
            start=candles_window_start(since, lookback_days),
            include_forming=False,
            candle_columns=("open_time", "open", "high", "low", "close", "volume"),
            indicator_columns=CONTEXT_IND_COLS,
        )
    except Exception:
        conn.rollback()
        return None
    if df.empty:
        return None
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True).dt.tz_localize(None)
    for c in df.columns:
        if c != "open_time":
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["high", "low", "close"]).reset_index(drop=True)


def floor_idx(times: np.ndarray, ts) -> int:
    """Index of the last CLOSED 1h candle before ``ts`` (floor-1 join,
    no lookahead). −1 if no candle exists."""
    floor64 = np.datetime64(pd.Timestamp(ts).floor("h"))
    return int(np.searchsorted(times, floor64, side="left")) - 1


def join_is_stale(times: np.ndarray, idx: int, ts) -> bool:
    floor64 = np.datetime64(pd.Timestamp(ts).floor("h"))
    return (floor64 - times[idx]) / np.timedelta64(1, "h") > MAX_JOIN_STALENESS_H


def load_regime(conn) -> tuple[np.ndarray, list[dict]]:
    """regime_history (ts = naive UTC) for regime_at lookups."""
    df = df_query(
        conn,
        "SELECT ts, regime, confidence FROM regime_history ORDER BY ts",
    )
    ts = pd.to_datetime(df["ts"]).values.astype("datetime64[ns]")
    return ts, df.to_dict("records")


def regime_at(r_ts: np.ndarray, r_rows: list[dict], ts64) -> tuple[dict | None, float]:
    i = int(np.searchsorted(r_ts, ts64, side="right")) - 1
    if i < 0:
        return None, 360.0
    age_min = float((ts64 - r_ts[i]) / np.timedelta64(1, "m"))
    return r_rows[i], age_min
