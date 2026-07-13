"""
core/market_utils.py
Shared trading utilities used across bots and strategies.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import re
from typing import TypedDict

import numpy as np
import pandas as pd

from core.candles import read_candles
from core.time import to_utc, utc_now

logger = logging.getLogger(__name__)

# ── Shared type definitions ────────────────────────────────────────────────


class SignalDict(TypedDict):
    strategy: str
    coin: str
    direction: str  # "LONG" | "SHORT"
    margin: str
    entry: float
    lev: str
    target1: float
    target2: float
    target3: float
    target4: float
    sl: float


class OBVResult(TypedDict):
    direction: int  # 1 = strong increase, -1 = strong decrease, 0 = neutral


_LEVERAGE_MAP: dict[str, int] | None = None
_LEVERAGE_MAP_PATH: str | None = None


def get_max_leverage(symbol: str, desired_leverage: int = 20) -> str:
    """
    Returns the leverage string (e.g. "20x") capped by max_leverage.json.
    Loads the file once and caches it in-process.
    """
    global _LEVERAGE_MAP, _LEVERAGE_MAP_PATH

    if _LEVERAGE_MAP is None:
        candidates = [
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "max_leverage.json"),
            "max_leverage.json",
        ]
        for path in candidates:
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        _LEVERAGE_MAP = json.load(f)
                    _LEVERAGE_MAP_PATH = path
                    break
                except Exception as e:
                    logger.error(f"Failed to load max_leverage.json from {path}: {e}")

        if _LEVERAGE_MAP is None:
            _LEVERAGE_MAP = {}

    max_lev: int = _LEVERAGE_MAP.get(symbol, 20)
    return f"{min(desired_leverage, max_lev)}x"


# A coin symbol is the atom of the ~40 f-string table names ``f"{sym}_{tf}"``
# scattered across the fleet (there is no single ``candles`` table). coins.json
# is written only by the two quoteAsset=USDT+PERPETUAL filters
# (1_data_ingestion + 6_housekeeping), so every real entry already matches this
# pattern — the validation is a no-op on live data and exists purely to close
# the one second-order identifier surface (AUDIT_TODO P3.3): a symbol that ever
# reached a table name unchecked. Enforced centrally here so all six former
# copies of load_coins inherit it.
_SYMBOL_RE = re.compile(r"[A-Z0-9]+")


def load_coins(path: str = "coins.json", *, usdt_only: bool = False, uppercase: bool = False) -> list[str]:
    """Loads and symbol-validates the coin list from coins.json.

    Central identifier hygiene (P3.3): each symbol must match ``[A-Z0-9]+``
    (after optional upper-casing). Non-conforming entries are dropped with an
    ERROR log line — never silently kept, never ``fillna``-substituted. On the
    live coins.json (uppercase USDT perpetuals) nothing is dropped, so callers
    on the data path (1_data_ingestion, T-092) see an identical list.

    Args:
        path: coins.json location.
        usdt_only: keep only ``*USDT`` symbols — the offline backtest/trainer
            callers that historically filtered locally.
        uppercase: upper-case each symbol before validating/returning — matches
            the ``c.upper()`` those callers applied. Off by default so the data
            path keeps the verbatim json contract.

    Returns the surviving symbols in file order (empty list on read/parse error,
    preserving the all-or-nothing contract 1_data_ingestion relies on).
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"Failed to load coins from {path}: {e}")
        return []

    raw = data.get("coins", data) if isinstance(data, dict) else data
    out: list[str] = []
    for entry in raw:
        sym = str(entry)
        if uppercase:
            sym = sym.upper()
        if not _SYMBOL_RE.fullmatch(sym):
            logger.error(f"load_coins: dropping non-conforming symbol {entry!r} from {path}")
            continue
        if usdt_only and not sym.endswith("USDT"):
            continue
        out.append(sym)
    return out


def is_trade_already_active(conn, coin: str, direction: str, strategy: str) -> bool:
    """Returns True if an active trade for this coin/direction/strategy already exists."""
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM active_trades_master
                WHERE coin = %s AND direction = %s AND strategy = %s AND status = 'WORKING'
            )
            """,
            (coin, direction, strategy),
        )
        return cursor.fetchone()[0]


# Hard limit of the LIVE trade_cooldowns.module column: character varying(10).
# The in-repo bootstrap DDLs say VARCHAR(50)/TEXT, but the live table predates
# them and CREATE TABLE IF NOT EXISTS never widens columns (DDL drift, see
# AUDIT_TODO P2.2). A longer tag makes update_cooldown throw
# StringDataRightTruncation — which silenced the Volume Indicator for five
# days (T-2026-CU-9050-024). Guarded here so the contract fails loudly and
# identically in every environment, not just on the live VPS.
COOLDOWN_MODULE_MAX_LEN = 10


def _check_module_tag(module: str) -> None:
    if len(module) > COOLDOWN_MODULE_MAX_LEN:
        raise ValueError(
            f"cooldown module tag '{module}' is {len(module)} chars — live "
            f"trade_cooldowns.module is varchar({COOLDOWN_MODULE_MAX_LEN}), "
            "shorten the tag (see T-2026-CU-9050-024)"
        )


def check_cooldown(conn, module: str, coin: str, direction: str, cd_hours: float) -> bool:
    """Returns True if the cooldown period has not yet elapsed (trade blocked).

    Nutzt timezone-aware UTC. Falls die DB einen naiven Timestamp zurückgibt,
    it is interpreted as UTC (stored that way historically).
    """
    _check_module_tag(module)
    cutoff = utc_now() - datetime.timedelta(hours=cd_hours)
    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT last_posted_at FROM trade_cooldowns
            WHERE module = %s AND coin = %s AND direction = %s
            """,
            (module, coin, direction),
        )
        row = cursor.fetchone()
    if row is None:
        return False
    # `trade_cooldowns.last_posted_at` existiert live als timestamptz und in den
    # Bootstrap-DDLs als naive Spalte (DDL-Drift P2.2) — to_utc normalisiert beide.
    return to_utc(row[0]) > cutoff


def update_cooldown(conn, module: str, coin: str, direction: str, commit: bool = True) -> None:
    """Upserts the cooldown timestamp for this module/coin/direction.

    commit=False lässt den Upsert in der offenen Transaktion des Callers
    (P1.7: der Orchestrator committed Cooldown + Tracking + Outbox atomar).
    """
    _check_module_tag(module)
    with conn.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO trade_cooldowns (module, coin, direction, last_posted_at)
            VALUES (%s, %s, %s, NOW())
            ON CONFLICT (module, coin, direction)
            DO UPDATE SET last_posted_at = NOW()
            """,
            (module, coin, direction),
        )
    if commit:
        conn.commit()


def calculate_obv(
    conn,
    symbol: str,
    open_time_start: datetime.datetime,
    open_time_end: datetime.datetime,
) -> int:
    """
    Calculates OBV divergence between two timestamps.
    Returns 1 (strong increase), -1 (strong decrease), or 0 (neutral).

    Loads only the relevant time span + buffer for baseline statistics
    (last ~60 days before the start timestamp), not the full history.
    """
    if not isinstance(open_time_start, datetime.datetime):
        open_time_start = pd.to_datetime(open_time_start)
    if not isinstance(open_time_end, datetime.datetime):
        open_time_end = pd.to_datetime(open_time_end)

    # Baseline is the OBV diff statistics (mean/std of hourly changes)
    # der vorangegangenen 60 Tage. This is sufficient as a robust reference period for
    # ±2σ bands and is much faster than the full history.
    baseline_start = open_time_start - pd.Timedelta(days=60)

    # R1 (T-2026-CU-9050-108): via core.candles, closed candles only. Both bounds
    # stay inclusive (>= start, <= end) and the frame arrives ASC, exactly as the old
    # query; the forming filter is a no-op here because open_time_end is a closed bar
    # (the caller passes a bar timestamp, and the detector frame now excludes forming).
    df = read_candles(
        conn,
        symbol,
        "1h",
        start=baseline_start,
        end=open_time_end,
        include_forming=False,
        columns=("open_time", "close", "volume"),
    ).set_index("open_time")
    if df.empty or len(df) < 10:
        return 0

    direction = np.sign(df["close"].diff().fillna(0))
    df["obv"] = (direction * df["volume"]).cumsum()

    period = df.loc[open_time_start:open_time_end]
    if period.empty:
        return 0

    obv_change = period["obv"].iloc[-1] - period["obv"].iloc[0]
    changes = df["obv"].diff()
    mean, std = changes.mean(), changes.std()

    if obv_change > mean + 2 * std:
        return 1
    if obv_change < mean - 2 * std:
        return -1
    return 0


def find_support_resistance_zones(
    df: pd.DataFrame,
    lookback_period: int = 2160,
    volume_multiplier: float = 2.5,
    zone_threshold: int = 5,
    min_distance_percent: float = 1.5,
) -> tuple[list[tuple[float, int]], list[tuple[float, int]]]:
    """
    Identifies support and resistance zones from OHLCV data.
    Returns (support_zones, resistance_zones) as sorted lists of (price, count).

    Algorithmus:
    1. Betrachte nur High-Volume-Kerzen (> volume_multiplier × Median).
    2. Für jeden passenden Preis: Wenn er nahe genug bei einer existierenden Zone
       liegt (innerhalb zone_threshold × price_std), Counter dieser Zone erhöhen.
       Sonst: neue Zone anlegen — aber nur, wenn sie weit genug von allen anderen
       entfernt ist (min_distance), um Überlappung zu vermeiden.
    3. Ergebnis: Zonen sortiert after Counter (häufigste zuerst = relevantester Support/Resistance).
    """
    df_recent = df.iloc[-lookback_period:].copy() if len(df) >= lookback_period else df.copy()

    vol_median = df_recent["volume"].median()
    current_close = float(df_recent["close"].iloc[-1])
    min_distance = current_close * (min_distance_percent / 100)
    price_std = df_recent["close"].pct_change().std()
    # Absolute matching tolerance: price_std is a pct-change,
    # we need a price distance. Multiplied by current_close gives
    # a sensible absolute value (e.g. 0.5% × zone_threshold).
    match_tolerance = current_close * price_std * zone_threshold if pd.notna(price_std) else min_distance * 0.5

    def find_zones(price_col: str, above: bool) -> list[tuple[float, int]]:
        zones: dict[float, int] = {}
        for _, row in df_recent.iterrows():
            if row["volume"] <= vol_median * volume_multiplier:
                continue
            price = float(row[price_col])
            if above and price <= current_close:
                continue
            if not above and price >= current_close:
                continue

            # 1. Passt der Preis zu einer existierenden Zone? → Counter erhöhen
            matched = next((z for z in zones if abs(z - price) <= match_tolerance), None)
            if matched is not None:
                zones[matched] += 1
                continue

            # 2. Keine passending Zone vorhanden. Neue Zone nur anlegen wenn sie
            #    nicht zu nah an einer existierenden ist (verhindert
            #    Überlappungen im "Grauzonen"-Bereich zwischen match_tolerance
            #    und min_distance).
            if any(abs(price - z) < min_distance for z in zones):
                continue

            zones[price] = 1

        return sorted(zones.items(), key=lambda x: x[1], reverse=True)

    return find_zones("low", above=False), find_zones("high", above=True)


def send_telegram(message: str, channel_id: int) -> None:
    """Queues a Telegram message into the outbox table for the bot to dispatch."""
    from core.database import get_db_connection

    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO telegram_outbox (channel_id, message) VALUES (%s, %s)",
                (channel_id, message),
            )
        conn.commit()


def calculate_pivots(df: pd.DataFrame, window: int = 5) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
    """
    Finds swing lows (support pivots) and swing highs (resistance pivots).
    Returns (troughs, peaks) as lists of (bar_index, price).
    """
    import scipy.signal

    highs = df["high"].values
    lows = df["low"].values
    peak_idx = scipy.signal.argrelextrema(highs, np.greater, order=window)[0]
    trough_idx = scipy.signal.argrelextrema(lows, np.less, order=window)[0]
    return (
        [(int(i), float(lows[i])) for i in trough_idx],
        [(int(i), float(highs[i])) for i in peak_idx],
    )
