"""ccxt_data.py — OHLCV -> ``date, close`` DataFrame for the GARCH module.

Replaces the upstream yfinance path (garchmethod used equities via yfinance).
The GARCH core only needs a ``date, close`` contract, so this is the whole
coupling surface: fetch OHLCV via ccxt, hand back two columns.

``ccxt`` is imported lazily so the rest of the package (and the DB-free tests)
import without it. ``normalize_prices`` is pure pandas and always importable —
that is what the CSV path and the tests share.
"""

from __future__ import annotations

import sys
import time

import pandas as pd

# Binance USD-M klines cap per request; ccxt paginates the rest.
_MAX_LIMIT = 1500


def normalize_prices(df: pd.DataFrame) -> pd.DataFrame:
    """Coerce an arbitrary price frame to the ``date, close`` contract:
    parse dates, drop NaN/non-positive closes, de-duplicate by date, sort. Pure
    pandas — no network, no ccxt."""
    cols = {c.lower().strip(): c for c in df.columns}
    date_col = next((cols[k] for k in ("date", "time", "timestamp") if k in cols), df.columns[0])
    px_col = next(
        (cols[k] for k in ("close", "price", "priceusd", "adj close", "adj_close") if k in cols),
        df.columns[1],
    )
    out = df[[date_col, px_col]].copy()
    out.columns = ["date", "close"]
    out["date"] = pd.to_datetime(out["date"])
    out["close"] = pd.to_numeric(out["close"], errors="coerce")
    out = out.dropna(subset=["date", "close"])
    out = out[out["close"] > 0]
    out = out.drop_duplicates(subset="date", keep="last").sort_values("date")
    return out.reset_index(drop=True)


def load_prices_csv(path: str) -> pd.DataFrame:
    """Load a ``date, close`` series from CSV (same contract as upstream --csv)."""
    return normalize_prices(pd.read_csv(path))


def fetch_ohlcv_df(
    symbol: str,
    exchange_id: str = "binanceusdm",
    timeframe: str = "1d",
    since_days: int | None = None,
    max_bars: int = 5000,
) -> pd.DataFrame:
    """Fetch OHLCV via ccxt and return a ``date, close`` DataFrame.

    Paginates on ``since`` until ``max_bars`` is reached or the exchange stops
    returning new bars. ``since_days`` limits history to the trailing N days
    (default: as far back as ``max_bars`` allows). Requires ``ccxt`` + network —
    called only from the CLI / study path, never from the DB-free tests.
    """
    try:
        import ccxt
    except ImportError:  # pragma: no cover - exercised only without ccxt
        sys.exit("ccxt not installed. Use --csv, or: pip install -r requirements-garch.txt")

    exchange = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    if timeframe not in getattr(exchange, "timeframes", {timeframe: None}):
        sys.exit(f"{exchange_id} has no timeframe {timeframe}")

    tf_ms = exchange.parse_timeframe(timeframe) * 1000
    now_ms = exchange.milliseconds()
    since = None
    if since_days is not None:
        since = now_ms - since_days * 86_400_000

    rows: list[list] = []
    while len(rows) < max_bars:
        batch = exchange.fetch_ohlcv(symbol, timeframe, since=since, limit=_MAX_LIMIT)
        if not batch:
            break
        rows += batch
        next_since = batch[-1][0] + tf_ms
        if since is not None and next_since <= since:
            break  # no forward progress -> stop (defensive against stuck cursor)
        since = next_since
        if len(batch) < _MAX_LIMIT:
            break  # exchange returned a short page -> we have reached the head
        time.sleep(exchange.rateLimit / 1000.0)

    if not rows:
        sys.exit(f"No OHLCV returned for {symbol} on {exchange_id}")

    # a batch appends whole -> keep the most recent max_bars (a trailing vol
    # forecast only ever wants the freshest history).
    if len(rows) > max_bars:
        rows = rows[-max_bars:]

    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"])
    df["date"] = pd.to_datetime(df["ts"], unit="ms")
    return normalize_prices(df[["date", "close"]])
