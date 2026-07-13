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


def get_live_price(symbol, conn=None):
    """Fetches the current live price from Binance.
    Bei Ausfall (Rate-Limit, Netzwerk-Error) Fallback auf den neuesten Close
    aus der lokalen 5m-Tabelle — besser als den ganzen Coin zu skippingn.
    """
    try:
        url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol}&interval=1m&limit=1"
        resp = requests.get(url, timeout=5).json()
        return float(resp[0][4])
    except Exception:
        if conn is None:
            return None
        # Fallback: neuester 5m-Close aus DB — forming candle bewusst inkludiert
        # (Live-Preis-Fallback, core.candles contract 2: include_forming=True).
        try:
            df = read_candles(conn, symbol, "5m", limit=1, include_forming=True, columns=("open_time", "close"))
            if not df.empty:
                return float(df["close"].iloc[-1])
        except Exception:
            conn.rollback()
        return None
