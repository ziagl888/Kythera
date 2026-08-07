"""Shared live-price helpers (Binance futures last-price, DB-close fallback).

Lifted verbatim from ``3_detectors.py`` (Block 4, T-2026-CU-9050-111) so the
AI bots (22/24/25/11) can import them — ``3_detectors`` lives in a numerically
named, non-importable module. ``3_detectors`` now re-exports these names, so the
detector cycle and its batch-ticker test keep resolving unchanged.

Contract: these are the ONLY sanctioned readers of a forming/live price outside
monitors 5/8 (core.candles contract 2). Detection runs on closed candles; the
live price is fetched separately for entry/target/proximity computation.
"""

import logging

import requests

from core.candles import read_candles

logger = logging.getLogger(__name__)


def get_live_prices_batch():
    """P2.44: fetch ALL futures last-prices in ONE Binance call.

    Replaces ~530 serial ``get_live_price`` klines calls per detector cycle (one
    per coin) with a single ``/fapi/v1/ticker/price`` request (returns every
    symbol). Returns ``{symbol: float(price)}``; on any failure it returns ``{}``
    and the caller falls back to the per-symbol ``get_live_price`` (HTTP → DB),
    so a batch outage degrades to the old behaviour rather than skipping coins.
    """
    try:
        url = "https://fapi.binance.com/fapi/v1/ticker/price"
        resp = requests.get(url, timeout=10).json()
        return {row["symbol"]: float(row["price"]) for row in resp}
    except Exception:
        logger.warning("Batch ticker/price fetch failed; falling back to per-coin price lookups", exc_info=True)
        return {}


def posting_anchor(symbol):
    """The live price to anchor a signal's geometry on, or None.

    The sanctioned entry anchor for the POSTING path: a bot that derives its
    targets and stop as PERCENTAGES has to hang them off the price the position
    actually opens at, not off the price that triggered its rule
    (T-2026-KYT-9050-115).

    It exists as its own function to own one safety rule in one place: ``conn`` is
    deliberately NOT forwarded to ``get_live_price``. That fallback calls
    ``conn.rollback()`` on a query error, which is connection-wide — and the bots
    using this (42, 43) write signals and prediction rows across a cycle and commit
    ONCE at the end, so a rollback from inside a price lookup would discard the
    whole batch. Losing the DB fallback costs one skipped candidate; taking it
    could cost every signal in the cycle. HTTP-only is the cheaper failure.

    Callers must treat None as "skip this candidate" (T-011) and must NOT fall back
    to a stale price of their own — that reintroduces the defect this replaced.

    NOTE for callers: this is one HTTP request per call. Resolve the bulk through
    ``get_live_prices_batch`` (P2.44) and use this only for the few symbols missing
    from an otherwise good batch, under an explicit per-cycle bound.
    """
    price = get_live_price(symbol)
    return float(price) if price else None


def get_live_price(symbol, conn=None):
    """Fetches the current live price from Binance.
    On failure (rate limit, network error) falls back to the newest close
    from the local 5m table — better than skipping the whole coin.
    """
    try:
        url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1m&limit=1"
        resp = requests.get(url, timeout=5).json()
        return float(resp[0][4])
    except Exception:
        if conn is None:
            return None
        # Fallback: newest 5m close from DB — forming candle deliberately included
        # (live-price fallback, core.candles contract 2: include_forming=True).
        try:
            df = read_candles(conn, symbol, "5m", limit=1, include_forming=True, columns=("open_time", "close"))
            if not df.empty:
                return float(df["close"].iloc[-1])
        except Exception:
            conn.rollback()
        return None
