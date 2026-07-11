"""Single source of truth for the Binance USDT-perpetual universe (coins.json).

Historically two processes fetched Binance ``exchangeInfo`` and each wrote
``coins.json`` with its own copy of the filter:

  * ``1_data_ingestion.update_trading_pairs`` (runs on every ingestion start)
  * ``6_housekeeping.update_coins_json``      (nightly 03:00 UTC + on start)

Two hand-kept copies of the same filter drift (the ETHU incident, 2026-07-06:
the ingestion copy still let ``status=TRADING`` non-USDT junk through — quote
"U"/"USD1" → symbol ``ETHU``, cross pairs ``ETHBTC``, quarterly futures
``*_260925``, TRADIFI perps ``XAUUSDT``/``COSTUSDT`` — 657 instead of 530
symbols, consumed fleet-wide). Both also wrote non-atomically
(``open('w')`` truncates the live file the instant it opens; a crash mid-dump
or a concurrent reader sees a partial/empty ``coins.json``).

This module is the ONE writer: one filter (P2.16), one atomic write via
tmp-file + ``os.replace`` so a reader always sees either the old or the new
complete file, never a torn one. It also exposes the shape predicate
``looks_like_usdt_perp`` used to keep the delisted-trade cleanup from
force-closing trades on non-Binance-perp symbols (P2.17).
"""

from __future__ import annotations

import json
import os
import re
import tempfile

import requests

# A Binance USDⓈ-M perpetual symbol is ``<BASE>USDT`` with an uppercase
# alphanumeric base (e.g. BTCUSDT, 1000SHIBUSDT). This deliberately excludes
# the junk that leaked in via delisting/cross pairs: ``XAUUSD`` (ends "USD"),
# ``ETHBTC`` (ends "BTC"), lowercase or separator-bearing garbage.
_USDT_PERP_SHAPE = re.compile(r"[A-Z0-9]+USDT")


def looks_like_usdt_perp(symbol: str) -> bool:
    """True if ``symbol`` has the shape of a Binance USDT perpetual.

    Shape-only check (no network): ``<BASE>USDT`` with an uppercase
    alphanumeric base. Used by the delisted-trade cleanup so it only ever
    force-closes trades on symbols the fleet actually trades — never on
    metals/forex/cross pairs (``XAUUSD``, ``ETHBTC``, …) that may still be
    live elsewhere (P2.17).
    """
    if not symbol:
        return False
    return _USDT_PERP_SHAPE.fullmatch(symbol) is not None


def filter_usdt_perpetuals(exchange_info: dict) -> list[str]:
    """Extract the actively-traded USDT perpetuals from a Binance
    ``exchangeInfo`` payload.

    The one canonical filter (P2.16): ``quoteAsset == 'USDT'`` and
    ``status == 'TRADING'`` and ``contractType == 'PERPETUAL'``. Both former
    writers converged on exactly this filter after the ETHU incident; it now
    lives here once so the two call sites cannot drift again.
    """
    return [
        s["symbol"]
        for s in exchange_info.get("symbols", [])
        if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING" and s.get("contractType") == "PERPETUAL"
    ]


def fetch_usdt_perpetual_symbols(base_url: str, *, timeout: int = 10) -> list[str]:
    """Fetch Binance ``exchangeInfo`` and return the filtered USDT-perp list.

    Raises on network/HTTP/parse errors — the caller decides the fallback
    (ingestion falls back to the on-disk list; housekeeping skips the refresh).
    """
    response = requests.get(base_url + "/fapi/v1/exchangeInfo", timeout=timeout)
    response.raise_for_status()
    return filter_usdt_perpetuals(response.json())


def write_coins_json_atomic(symbols: list[str], path: str = "coins.json") -> None:
    """Write ``symbols`` to ``path`` atomically (tmp-file + ``os.replace``).

    A concurrent reader (delisted cleanup, gap-filler, any bot's
    ``load_coins``) always sees either the previous complete file or the new
    complete file — never the truncated window a plain ``open(path, 'w')``
    exposes for the duration of the dump. The tmp-file is created in the same
    directory as ``path`` so ``os.replace`` stays on one filesystem (its
    atomicity guarantee, on Windows too).
    """
    directory = os.path.dirname(os.path.abspath(path))
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".coins.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(symbols, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def refresh_coins_json(base_url: str, path: str = "coins.json", *, timeout: int = 10) -> list[str]:
    """Fetch the current USDT-perp universe and write it atomically.

    Returns the symbol list. Raises on fetch failure (nothing is written) —
    the atomic write only happens once a full, valid list is in hand, so a
    failed refresh never truncates the live ``coins.json``.
    """
    symbols = fetch_usdt_perpetual_symbols(base_url, timeout=timeout)
    write_coins_json_atomic(symbols, path)
    return symbols
