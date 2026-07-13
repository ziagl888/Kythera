"""
tools/research_dataset_common.py — geteilte Helfer der Research-Dataset-Builder
(pex1/fmr1/trm1/fif1_build_dataset.py). Muster und TZ-Konventionen aus
tools/aim2_build_dataset.py; Labels kommen IMMER aus simulate_exit
(tools/walkforward_sim.py — wick-aware First-Touch, SL-first, Fees).
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

# cp1252-Konsole: Sonderzeichen in Ausgaben dürfen den Lauf nicht abbrechen
# (gleicher Fix wie tools/retrain_from_replay.py, 13ce748).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from core.candles import read_candles_with_indicators  # noqa: E402
from core.research_features import CONTEXT_SQL_SELECT  # noqa: E402

# CONTEXT_SQL_SELECT ist ein "i.<col>, …"-SQL-Fragment; für read_candles_with_
# indicators brauchen wir die reinen Spaltennamen (Präfix/Whitespace entfernt).
CONTEXT_IND_COLS = [c.strip().split(".")[-1] for c in CONTEXT_SQL_SELECT.split(",") if c.strip()]

STAGING_DIR = os.getenv("KYTHERA_STAGING_DIR", r"C:\Users\Michael\Documents\_X\staging_models")
REPLAY_DIR = os.getenv("KYTHERA_REPLAY_DIR", os.path.join(STAGING_DIR, "replay"))

LOCAL_TZ = "Europe/Bucharest"      # PG-Lokalzeit der *_trades_master-Tabellen (vermessen 2026-07-05)
MAX_JOIN_STALENESS_H = 3           # Kerzen-Lücke → Event verwerfen
MIN_WINDOW = 60                    # Mindest-Kerzen vor dem Event
WINDOW_CANDLES = 500               # Smart-Targets-Fenster


def log(msg: str) -> None:
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def set_low_priority() -> None:
    """VPS läuft an der Lastgrenze — Builder laufen mit BELOW_NORMAL."""
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


def to_utc_naive(series: pd.Series) -> pd.Series:
    """Naive Lokalzeit (Europe/Bucharest) → naive UTC (aim2-Konvention)."""
    s = pd.to_datetime(series, errors="coerce")
    s = s.dt.tz_localize(LOCAL_TZ, nonexistent="shift_forward", ambiguous="NaT")
    return s.dt.tz_convert("UTC").dt.tz_localize(None)


def candles_window_start(since: str, lookback_days: int):
    """Untere Fenstergrenze als aware Datetime für core.candles.

    Reproduziert das frühere ``%s::timestamptz - INTERVAL 'N days'`` der Builder-
    SQL: ``since`` wurde DB-seitig in der Session-TZ (PG-Lokal == LOCAL_TZ) als
    timestamptz interpretiert. Wir lokalisieren identisch und ziehen die Tage ab.
    Das ist nur eine Warmup-Untergrenze weit vor den Events — DST-Granularität
    (≤1h) ist immateriell, und die Bucharest-Lesart schneidet nie SPÄTER als die
    alte SQL, verliert also keine Kerzen.
    """
    ts = pd.Timestamp(since)
    if ts.tzinfo is None:
        ts = ts.tz_localize(LOCAL_TZ, nonexistent="shift_forward", ambiguous=True)
    return (ts - pd.Timedelta(days=int(lookback_days))).to_pydatetime()


def load_candles_ctx(conn, symbol: str, since: str, lookback_days: int = 30) -> pd.DataFrame | None:
    """1h-Kerzen + Kontext-Indikatoren (CONTEXT_SQL_SELECT-Join), ASC, naive UTC.

    Über core.candles: GESCHLOSSENE Kerzen (include_forming=False). Die Caller
    schneiden ohnehin per floor_idx auf die letzte geschlossene Kerze vor dem
    Event — die forming Tail-Zeile hätten sie nie gewählt; der Wechsel ist
    vertragskompatibel und entfernt einen latenten R1-Repaint (Report §3)."""
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
    """Index der letzten GESCHLOSSENEN 1h-Kerze vor ``ts`` (floor-1-Join,
    kein Lookahead). −1 wenn keine Kerze existiert."""
    floor64 = np.datetime64(pd.Timestamp(ts).floor("h"))
    return int(np.searchsorted(times, floor64, side="left")) - 1


def join_is_stale(times: np.ndarray, idx: int, ts) -> bool:
    floor64 = np.datetime64(pd.Timestamp(ts).floor("h"))
    return (floor64 - times[idx]) / np.timedelta64(1, "h") > MAX_JOIN_STALENESS_H


def load_regime(conn) -> tuple[np.ndarray, list[dict]]:
    """regime_history (ts = naive UTC) für regime_at-Lookups."""
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
