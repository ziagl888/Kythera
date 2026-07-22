"""ccxt_data.py — OHLC klines for the regime study.

The regime classifier needs BTC OHLC (returns + True-Range/ATR) and BTCDOM close
(dominance 24h change). This module is the whole network coupling surface: fetch
OHLCV via ccxt, hand back a ``open_time, open, high, low, close, volume`` frame.

``ccxt`` is imported lazily so the pure study modules (and the DB-free tests)
import without it. ``normalize_ohlc`` is pure pandas and always importable — that
is what the CSV path and the tests share.

Page-forward pagination (``since = now - max_bars * tf``) mirrors the GARCH
package; ``since=None`` only ever returns the most recent single page.
"""

from __future__ import annotations

import sys
import time

import pandas as pd

# binanceusdm caps klines at 1000 per request; ccxt paginates the rest.
_PAGE = 1000

OHLC_COLUMNS = ("open_time", "open", "high", "low", "close", "volume")


def normalize_ohlc(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce an OHLCV frame to the ``open_time, open, high, low, close, volume``
    contract: parse the timestamp, numeric-coerce, drop bad closes, de-dup by
    time, sort. Pure pandas — no network."""
    cols = {c.lower().strip(): c for c in df.columns}
    ts_col = next((cols[k] for k in ("open_time", "date", "time", "timestamp") if k in cols), df.columns[0])
    out = pd.DataFrame({"open_time": pd.to_datetime(df[ts_col])})
    for name in ("open", "high", "low", "close", "volume"):
        src = cols.get(name)
        out[name] = pd.to_numeric(df[src], errors="coerce") if src is not None else float("nan")
    out = out.dropna(subset=["close"])
    out = out[out["close"] > 0]
    out = out.drop_duplicates(subset="open_time", keep="last").sort_values("open_time")
    return out.reset_index(drop=True)


def load_ohlc_csv(path: str) -> pd.DataFrame:
    """Load an OHLC frame from CSV (offline path for the tests / reruns)."""
    return normalize_ohlc(pd.read_csv(path))


def fetch_ohlc(
    symbol: str,
    exchange_id: str = "binanceusdm",
    timeframe: str = "15m",
    max_bars: int = 40_000,
) -> pd.DataFrame:
    """Fetch OHLCV via ccxt and return the OHLC contract frame.

    Pages forward from ``max_bars`` ago until the head of the series is reached
    or ``max_bars`` rows are collected. Requires ``ccxt`` + network — called only
    from the CLI / study path, never from the DB-free tests.
    """
    try:
        import ccxt
    except ImportError:  # pragma: no cover - exercised only without ccxt
        sys.exit("ccxt not installed. pip install -r tools/research/regime_switch/requirements-regime.txt")

    ex = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    if timeframe not in getattr(ex, "timeframes", {timeframe: None}):
        sys.exit(f"{exchange_id} has no timeframe {timeframe}")

    tf_ms = ex.parse_timeframe(timeframe) * 1000
    since = ex.milliseconds() - max_bars * tf_ms
    rows: list[list] = []
    while len(rows) < max_bars:
        batch = ex.fetch_ohlcv(symbol, timeframe, since=since, limit=_PAGE)
        if not batch:
            break
        rows += batch
        nxt = batch[-1][0] + tf_ms
        if nxt <= since:
            break  # no forward progress -> stop (defensive against a stuck cursor)
        since = nxt
        if len(batch) < _PAGE:
            break  # short page -> reached the head of the series
        time.sleep(ex.rateLimit / 1000.0)

    if not rows:
        sys.exit(f"no OHLCV for {symbol} {timeframe} on {exchange_id}")

    df = pd.DataFrame(rows[-max_bars:], columns=["ts", "open", "high", "low", "close", "volume"])
    df["open_time"] = pd.to_datetime(df["ts"], unit="ms")
    return normalize_ohlc(df[list(OHLC_COLUMNS)])
