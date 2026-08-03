# core/funding_features.py
"""Shared funding feature builder — ONE source for studies, trainers and bots.

Origin: Report 21 Addendum 2 (ABR1-LONG study 2026-07-06). The feature
definitions validated there are held canonical here, so that
upcoming retrains (RUB2, EPD2, …) use exactly the same quantities as the
study and the live bot (no train/serve skew — same rule as
core/mis_features.py and core/aim2_features.py).

Data source offline: table ``funding_rates`` (fully backfilled via
``tools/backfill_funding_rates.py``; 8h grid). Live, the bot fetches the same
values via REST (see 18_ai_abr1_bot.get_funding_24h_bps).

All features are as-of: only funding rates whose funding_time is
STRICTLY before the event timestamp are included — no lookahead.

Validated thresholds (ABR family, as of 2026-07-06):
  * LONG gate:   fund_24h > +3.0 bps  (+1.12 %/trade, 74 % WR)
  * SHORT veto:  fund_24h > +1.5 bps  (−1.21 %/trade in the zone)
Reference: Binance default funding = +1.0 bps/8h — ~75 % of values cling
there, the signal sits STRICTLY above/below.
"""

from __future__ import annotations

import datetime

import numpy as np
import pandas as pd

FUNDING_FEATURES = [
    "fund_last",  # last settled rate (bps)
    "fund_24h",  # mean of last 3 rates (bps) — gate/veto quantity
    "fund_72h",  # mean of last 9 rates (bps)
    "fund_7d_cum",  # sum of last 21 rates (bps)
    "fund_pctl_90d",  # percentile of the last rate vs. its own 90d history
    "fund_trend",  # fund_24h − fund_72h (bps)
]

#: Minimum number of historical rates before features are computed (7 days).
MIN_HISTORY = 21


def load_funding(conn, symbols: list[str], since=None) -> dict[str, pd.DataFrame]:
    """Loads the funding history per symbol from ``funding_rates`` (ascending).

    since: optional lower bound (tz-aware). Live bots use it to bound the
    load — funding_features_asof uses at most the last 270 rates (~90d),
    pulling the full history per trigger would be wasted DB work.
    Trainers/replays omit since (as-of over the entire time range).
    """
    query = "SELECT symbol, funding_time, funding_rate FROM funding_rates WHERE symbol = ANY(%(syms)s)"
    params: dict = {"syms": list(symbols)}
    if since is not None:
        query += " AND funding_time >= %(since)s"
        params["since"] = since
    query += " ORDER BY symbol, funding_time"
    fr = pd.read_sql_query(query, conn, params=params)
    fr["funding_time"] = pd.to_datetime(fr["funding_time"], utc=True)
    return {s: g.reset_index(drop=True) for s, g in fr.groupby("symbol")}


def funding_features_asof(by_sym: dict[str, pd.DataFrame], symbol: str, ts_utc) -> dict:
    """The 6 FUNDING_FEATURES for an event at timestamp ``ts_utc`` (tz-aware).

    Returns {} if the symbol is missing or history < MIN_HISTORY — the caller
    decides (trainer: drop the row; gate: fail-closed/-open per policy).
    """
    g = by_sym.get(symbol)
    if g is None:
        return {}
    i = int(np.searchsorted(g["funding_time"].values, np.datetime64(pd.Timestamp(ts_utc))))
    if i < MIN_HISTORY:
        return {}
    rates = g["funding_rate"].values[:i] * 1e4  # → bps
    last, m3, m9 = rates[-1], rates[-3:].mean(), rates[-9:].mean()
    hist90 = rates[-270:]
    return {
        "fund_last": float(last),
        "fund_24h": float(m3),
        "fund_72h": float(m9),
        "fund_7d_cum": float(rates[-21:].sum()),
        "fund_pctl_90d": float((hist90 <= last).mean() * 100),
        "fund_trend": float(m3 - m9),
    }


# --- Settlement-bound cache for high-frequency callers (T-2026-CU-9050-055) ---
#
# ``funding_features_asof`` depends on the timestamp EXCLUSIVELY via the
# searchsorted cut — via the number of rates with funding_time < ts. As long as
# no new settlement is added, the result is constant: the cut stays the
# same, and all aggregates are suffixes (rates[-3:], rates[-270:], …), so they
# don't depend on the load's moving ``since`` lower bound.
#
# The cache key therefore comes from the DATA, not the wall clock: an
# entry is valid up to the settlement that can next change the result
# (see ``next_feature_change``). Two error classes that a clock-bound key
# (e.g. "one hour") would have are eliminated by this:
#
#   * Settlements that don't land on a full hour. Nothing enforces
#     that — ``tools/backfill_funding_rates.py`` writes ``funding_time`` at
#     full millisecond resolution. A rate at 12:30 would have stayed
#     invisible under an hour key until 13:00.
#   * Ingestion lag. If the due row isn't there yet, the entry has
#     already expired: it gets reloaded until it appears — then the fresh
#     ``funding_time`` pushes the boundary further out. The cache corrects
#     itself instead of betting on an ingestion SLA.
#
# A naive time TTL can do neither: it can span a settlement boundary
# and serve the model stale funding — a break of trainer parity
# (train == serve == replay).
# as-of uses at most the last 270 rates (rates[-270:] for fund_pctl_90d). At
# 8h cadence that's exactly 90 days — 95d would give only 5 days of buffer, a
# coin with a >5d cumulative funding gap in its last 270 rates would get fewer
# than 270 samples live and would deviate slightly from the trainer in
# fund_pctl_90d (which computes over the full history). 110d gives 20 days of
# gap buffer above the 90d minimum.
CACHE_SINCE_DAYS = 110
#: How many of the most recent gaps go into the interval estimate.
CACHE_INTERVAL_SAMPLES = 8

#: symbol → (valid_until = next due settlement, features)
_CACHE: dict[str, tuple[pd.Timestamp, dict]] = {}


def clear_funding_cache() -> None:
    """Test-only / process reset."""
    _CACHE.clear()


def next_feature_change(g: pd.DataFrame, ts_utc) -> pd.Timestamp | None:
    """Until when the as-of result for ``ts_utc`` stays unchanged.

    ``funding_features_asof`` cuts with ``searchsorted(..., 'left')``: the
    rates with ``funding_time < ts`` go in. The result only flips once ts
    crosses the NEXT rate. That rate is either already in the data (then
    it's the boundary — even if it doesn't land on a full hour), or it's
    not settled yet: then it's estimated from the history.

    The estimate deliberately takes the **minimum** of the most recent gaps, not
    the median. The two error directions aren't equally costly:

      * Estimated too SHORT → the entry expires too early, one extra
        DB roundtrip. Costs time, never correctness.
      * Estimated too LONG → the cache sits past a real settlement and
        serves a stale value. Exactly the parity break this cache is
        meant to prevent.

    If a coin shortens its cadence (Binance 8h → 4h/1h) or an ingestion gap
    distorts the most recent gaps, a median would overestimate the next
    interval by hours. The minimum can't overestimate the OBSERVED gaps —
    no history-based estimate can foresee the very first rate of a suddenly
    shorter cadence (an onset overshoot remains), but from the second short
    rate onward the minimum catches up, whereas a median window would still
    hang on the old value for hours.

    ``None`` for too-short a history — without two rates no interval can be
    determined, so nothing is cached.
    """
    ft = g["funding_time"]
    if len(ft) < 2:
        return None
    i = int(ft.searchsorted(pd.Timestamp(ts_utc), side="left"))
    if i < len(ft):
        return ft.iloc[i]
    step = ft.diff().dropna().iloc[-CACHE_INTERVAL_SAMPLES:].min()
    if pd.isna(step) or step <= pd.Timedelta(0):
        return None
    return ft.iloc[-1] + step


def funding_features_cached(conn, symbol: str, ts_utc: datetime.datetime, loader=load_funding) -> dict:
    """Like ``funding_features_asof``, but without repeating the DB roundtrip
    as long as no new settlement can change the result. The values are
    identical to the uncached call (rationale above).

    ``loader`` is injectable so the cache stays testable without a DB.
    """
    hit = _CACHE.get(symbol)
    if hit is not None and pd.Timestamp(ts_utc) <= hit[0]:
        return hit[1]

    by_sym = loader(conn, [symbol], since=ts_utc - datetime.timedelta(days=CACHE_SINCE_DAYS))
    feats = funding_features_asof(by_sym, symbol, ts_utc)

    g = by_sym.get(symbol)
    valid_until = next_feature_change(g, ts_utc) if g is not None else None
    # Do NOT cache an EMPTY result (history < MIN_HISTORY): the next rate that
    # lifts the coin over the threshold can land earlier than the estimated
    # interval, and until then `{}` would be served — exactly when the coin
    # becomes tradeable. Same as the late-row case: better to reload every tick
    # until real features are there.
    if feats and valid_until is not None and pd.Timestamp(ts_utc) <= valid_until:
        _CACHE[symbol] = (valid_until, feats)
    else:
        # History too short (no interval determinable) OR the settlement is
        # overdue because the row hasn't been ingested yet. In the second case
        # the entry would have expired immediately anyway — the clock runs
        # forward, an expired entry is never served. The `pop` is thus hygiene
        # (and a safety net against an NTP rollback), not the load-bearing guard.
        # The load-bearing guard is the `<=` comparison above.
        _CACHE.pop(symbol, None)
    return feats
