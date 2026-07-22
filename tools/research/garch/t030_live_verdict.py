"""t030_live_verdict.py — GARCH vol-targeting LIVE verdict on real Kythera trades.

The open half of T-022 (T-2026-KYT-9050-030): does GARCH vol-targeting improve the
realized risk-adjusted returns of Kythera's *edge-positive* bots? This is a
READ-ONLY study — it only measures, it never wires sizing into any bot.

What it does
------------
1. Pull realized trades from ``closed_ai_signals`` for a set of edge-positive
   model tags (real-geometry exits only, no synthetic LEGACY ±2.5% rows), and
   compute each trade's direction-adjusted realized return
   ``r = sign * (close_price - entry) / entry``.
2. For every distinct coin, load its DAILY closed candles via the shared reader
   (``core.candles.read_candles``, ``include_forming=False``) and run the merged
   GARCH harness (``tools.research.garch.garch_forecast.walkforward_garch``,
   ``min_train=500``) once to get a 1-bar-ahead annualized-vol forecast series.
3. For each trade, look up the forecast row with the latest candle date STRICTLY
   BEFORE the trade's entry date (zero lookahead: that forecast used only candles
   whose close is at/before entry-day 00:00), and derive a size multiplier
   ``size_from_vol(fcast_vol_ann, target_vol, cap [0.25, 2.0])``.
4. Compare FIXED (1x) vs VOL-TARGETED (m_i x r_i) on the *same* trade subset:
   per-bot-leg, per-bot, and pooled Sharpe / max-drawdown / worst-month / mean,
   then a PULLS / MIXED / NO-PULL verdict.

target_vol calibration (the honest test)
----------------------------------------
Crypto daily vol runs ~40-95% annualized, so the harness default target_vol=15%
would peg almost every multiplier to the 0.25 floor — a uniform 4x deleverage,
not a regime-reallocation test. We instead set ``target_vol`` to the sample
MEDIAN forecast vol, so the multiplier centers on ~1.0 (vol-targeted book keeps
roughly the same average size as fixed) and the [0.25, 2.0] clip only bites in
calm/storm extremes. That isolates the reallocation effect the idea is about.
The naive target_vol=15 case is reported as a sensitivity footnote.

HARD GATES honored: read-only DB (SELECT only, ``set_session(readonly=True)`` +
statement_timeout), no writes, no artifact promotion, no gate flips, no live
wiring. Run one job at a time (CPU-aware); ``--limit-coins`` samples first.

Usage (VPS, DB reachable):
  python tools/research/garch/t030_live_verdict.py --limit-coins 40        # sample
  python tools/research/garch/t030_live_verdict.py --json-out result.json  # full
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

# repo root on the path so ``core.*`` imports resolve when run as a script
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
for _p in (_REPO_ROOT, _HERE):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from garch_forecast import walkforward_garch  # noqa: E402  (tools/research/garch local)
from vol_target import MAX_LEVERAGE, MIN_SIZE, size_from_vol  # noqa: E402

# Edge-positive model tags (confirmed empirically from realized WR/mean-PnL over
# real-geometry exits, 2026-07-23; the T-022 expected set AIM2/EPD/MIS/RUB2-SHORT/
# MAX1 plus their positive legs). Direction is filtered per-leg below.
EDGE_MODELS = [
    "AIM2",
    "EPD1",
    "EPD3",
    "MIS1-72H",
    "MIS1-168H",
    "MIS1-8H",
    "MIS1-24H",
    "MIS2-24H",
    "MIS2-72H",
    "MIS2-8H",
    "RUB2",
    "MAX1",
]

# Statuses that are NOT real per-trade geometry (synthetic ±2.5% legacy closes,
# cleanup rows, unfilled entries) — excluded from the realized-return sample.
_EXCLUDE_STATUS = ("DELISTED / CLEANUP", "ENTRY_NOT_FILLED")

MIN_TRAIN = 500  # daily bars of warmup before the first GARCH forecast
MIN_LEG_TRADES = 50  # keep only edge legs with >= this many forecast-covered trades
PERIODS_PER_YEAR_DAILY = 365


# --------------------------------------------------------------------- DB layer
def _connect_readonly(statement_timeout_ms: int = 20000):
    """Open a READ-ONLY psycopg2 connection to the live cryptodata DB.

    Hard gate: ``set_session(readonly=True)`` makes the server reject any write
    at the transaction level, and a statement_timeout caps runaway scans. This
    study runs SELECT only.
    """
    import psycopg2

    from core.config import DB_HOST, DB_NAME, DB_PASSWORD, DB_PORT, DB_USER

    conn = psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASSWORD,
        host=DB_HOST,
        port=DB_PORT,
        options=f"-c statement_timeout={statement_timeout_ms}",
    )
    conn.set_session(readonly=True)
    return conn


def load_edge_trades(conn, models=EDGE_MODELS) -> pd.DataFrame:
    """Realized trades for the edge model tags, real-geometry exits only.

    Returns columns: model, symbol, direction, open_time, entry, close_price, r
    where ``r`` is the direction-adjusted realized fractional return.
    """
    sql = """
        select model, symbol, direction, open_time, entry, close_price,
               case when direction = 'LONG' then (close_price - entry) / entry
                    else (entry - close_price) / entry end as r
        from closed_ai_signals
        where model = any(%s)
          and status not like 'LEGACY%%'
          and status <> all(%s)
          and entry is not null and close_price is not null
          and entry > 0 and close_price > 0
        order by open_time
    """
    df = pd.read_sql(sql, conn, params=(models, list(_EXCLUDE_STATUS)))
    # normalize to naive-UTC (candles come back tz-aware; trades are naive/DB-UTC)
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True).dt.tz_localize(None)
    return df


def load_daily_close(conn, symbol: str) -> pd.DataFrame | None:
    """Full daily closed-candle series for ``symbol`` via the shared reader,
    shaped as the harness expects (``date``, ``close``). None on any failure /
    empty table (newer coins without a 1d table)."""
    from core.candles import read_candles

    try:
        df = read_candles(conn, symbol, "1d", include_forming=False)
    except Exception:
        conn.rollback()
        return None
    if df is None or df.empty or "close" not in df.columns:
        return None
    out = df[["open_time", "close"]].rename(columns={"open_time": "date"}).copy()
    out["date"] = pd.to_datetime(out["date"], utc=True).dt.tz_localize(None)
    out = out.dropna(subset=["close"]).reset_index(drop=True)
    return out


# ----------------------------------------------------------------- GARCH per coin
def forecast_series_for_coin(prices: pd.DataFrame, min_train: int = MIN_TRAIN) -> pd.DataFrame | None:
    """Walk-forward GARCH forecast series for one coin, or None if too short.

    Returns a frame with ``date`` and ``fcast_vol_ann`` (annualized %), dropna'd.
    """
    if prices is None or len(prices) < min_train + 11:
        return None
    try:
        wf = walkforward_garch(prices, periods_per_year=PERIODS_PER_YEAR_DAILY, min_train=min_train)
    except ValueError:
        return None
    wf = wf.dropna(subset=["fcast_vol_ann"])[["date", "fcast_vol_ann"]]
    if wf.empty:
        return None
    return wf.sort_values("date").reset_index(drop=True)


def asof_forecast(fc: pd.DataFrame, entry_time: pd.Timestamp) -> float:
    """Forecast vol (annualized %) as-of a trade entry: the latest forecast row
    whose candle date is STRICTLY before the entry day → zero lookahead. NaN if
    no such row (entry precedes the warmup)."""
    entry_day = pd.Timestamp(entry_time).normalize()
    prior = fc[fc["date"] < entry_day]
    if prior.empty:
        return float("nan")
    return float(prior["fcast_vol_ann"].iloc[-1])


# --------------------------------------------------------------------- metrics
def _equity_maxdd(r_frac: np.ndarray) -> tuple[float, float]:
    """ADDITIVE cumulative return (fixed-fractional book) and its max drawdown,
    both as fractions of a fixed per-trade notional.

    Additive — not compounded — because these are discrete signals that overlap
    in time across ~593 coins; sequential compounding of an overlapping trade
    stream is fictional (it produced a 12M-x equity / -98% DD artifact). Each
    trade risks a fixed notional; total book PnL is the sum of per-trade returns.
    """
    if len(r_frac) == 0:
        return float("nan"), float("nan")
    eq = np.cumsum(r_frac)  # cumulative return in units of one fixed-notional trade
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak).min()  # deepest peak-to-trough, in return-points (fraction)
    return float(eq[-1]), float(dd)


def _worst_month(r_frac: np.ndarray, dates: pd.Series) -> float:
    """Worst calendar-month summed return (fraction, fixed-fractional book)."""
    if len(r_frac) == 0:
        return float("nan")
    s = pd.Series(r_frac, index=pd.to_datetime(dates.values))
    m = s.resample("ME").sum()
    return float(m.min()) if len(m) else float("nan")


def leg_metrics(r_frac: np.ndarray, dates: pd.Series) -> dict:
    """Per-trade Sharpe / mean / std / maxDD / worst-month / final equity for a
    fractional return sequence ordered by entry time. Sharpe is per-trade
    (mean/std); the annualization factor is identical across fixed/vol so it
    cancels in the delta."""
    r = np.asarray(r_frac, dtype=float)
    dates = pd.Series(pd.to_datetime(pd.Series(dates).values))
    mask = ~np.isnan(r)
    r = r[mask]
    dates = dates[np.asarray(mask)].reset_index(drop=True)  # keep r and dates aligned
    if len(r) == 0:
        return {}
    mean = float(r.mean())
    sd = float(r.std(ddof=1)) if len(r) > 1 else float("nan")
    sharpe = mean / sd if sd and sd > 0 else float("nan")
    total_ret, maxdd = _equity_maxdd(r)
    wm = _worst_month(r, dates)
    return {
        "n": int(len(r)),
        "mean_pct": round(100 * mean, 4),
        "sd_pct": round(100 * sd, 4),
        "sharpe": round(sharpe, 4) if not np.isnan(sharpe) else None,
        "win_rate_pct": round(100 * float((r > 0).mean()), 2),
        "max_drawdown_pp": round(100 * maxdd, 2),  # additive DD, return-points
        "worst_month_pp": round(100 * wm, 2) if not np.isnan(wm) else None,
        "total_return_pp": round(100 * total_ret, 1),  # sum of per-trade returns
    }


def _delta(a: dict, b: dict, key: str):
    x, y = a.get(key), b.get(key)
    if x is None or y is None:
        return None
    return x - y


def verdict(fixed: dict, volt: dict) -> dict:
    """PULLS / MIXED / NO-PULL from a fixed-vs-vol stat pair.

    Mirrors compare.verdict_from_stats thresholds (Sharpe +0.10 delta; DD and
    worst-month may not worsen by > 2pp).
    """
    sd = _delta(volt, fixed, "sharpe")
    dd = _delta(volt, fixed, "max_drawdown_pp")
    wm = _delta(volt, fixed, "worst_month_pp")
    if sd is None:
        return {"verdict": "NO-DATA"}
    dd_ok = dd is None or dd >= -2.0
    wm_ok = wm is None or wm >= -2.0
    if sd >= 0.10 and dd_ok and wm_ok:
        v = "PULLS"
    elif sd <= 0:
        v = "NO-PULL"
    else:
        v = "MIXED"
    return {
        "verdict": v,
        "sharpe_delta": round(sd, 4),
        "max_dd_delta_pct": round(dd, 2) if dd is not None else None,
        "worst_month_delta_pct": round(wm, 2) if wm is not None else None,
    }


# ------------------------------------------------------------------------ driver
def run(limit_coins: int | None, statement_timeout_ms: int, verbose: bool) -> dict:
    conn = _connect_readonly(statement_timeout_ms)
    try:
        with conn.cursor() as cur:
            cur.execute("show transaction_read_only")
            assert cur.fetchone()[0] == "on", "connection is NOT read-only — abort"

        trades = load_edge_trades(conn)
        if verbose:
            print(f"loaded {len(trades)} edge trades across {trades['symbol'].nunique()} coins", file=sys.stderr)

        # Order coins by trade count so a --limit-coins sample covers the most trades.
        coin_counts = trades["symbol"].value_counts()
        coins = list(coin_counts.index)
        if limit_coins:
            coins = coins[:limit_coins]
        coin_set = set(coins)

        # Build the as-of forecast per trade (only for sampled coins).
        fc_cache: dict[str, pd.DataFrame | None] = {}
        for i, sym in enumerate(coins):
            prices = load_daily_close(conn, sym)
            fc_cache[sym] = forecast_series_for_coin(prices)
            if verbose and (i + 1) % 25 == 0:
                print(f"  garch {i + 1}/{len(coins)} coins", file=sys.stderr)

        sub = trades[trades["symbol"].isin(coin_set)].copy()
        fvols = []
        for row in sub.itertuples(index=False):
            fc = fc_cache.get(row.symbol)
            fvols.append(asof_forecast(fc, row.open_time) if fc is not None else float("nan"))
        sub["fcast_vol_ann"] = fvols
        covered = sub[sub["fcast_vol_ann"].notna() & (sub["fcast_vol_ann"] > 0)].copy()
    finally:
        conn.close()

    if covered.empty:
        return {"error": "no trades with a valid as-of GARCH forecast", "n_trades_sampled": int(len(sub))}

    # Calibrate target_vol to the pooled median forecast → multiplier centers ~1.0.
    target_vol = float(covered["fcast_vol_ann"].median())
    covered["mult"] = covered["fcast_vol_ann"].apply(lambda v: size_from_vol(v, target_vol))
    # Naive target=15 sensitivity (the pure-deleverage artifact).
    covered["mult_naive15"] = covered["fcast_vol_ann"].apply(lambda v: size_from_vol(v, 15.0))

    covered = covered.sort_values("open_time").reset_index(drop=True)

    def _pair(df: pd.DataFrame, mult_col: str = "mult") -> dict:
        r = df["r"].to_numpy(dtype=float)
        m = df[mult_col].to_numpy(dtype=float)
        fixed = leg_metrics(r, df["open_time"])
        volt = leg_metrics(m * r, df["open_time"])
        return {"fixed": fixed, "vol_targeted": volt, "verdict": verdict(fixed, volt)}

    # Per bot-leg (model, direction), per bot (model), pooled.
    by_leg: dict[str, dict] = {}
    for (model, direction), g in covered.groupby(["model", "direction"]):
        if len(g) < MIN_LEG_TRADES:
            continue
        # keep only edge-positive legs (positive realized mean in the covered set)
        if g["r"].mean() <= 0:
            continue
        by_leg[f"{model}-{direction}"] = _pair(g)

    by_bot: dict[str, dict] = {}
    for model, g in covered.groupby("model"):
        if len(g) < MIN_LEG_TRADES or g["r"].mean() <= 0:
            continue
        by_bot[model] = _pair(g)

    pooled = _pair(covered)
    pooled_naive = _pair(covered, mult_col="mult_naive15")

    # Median-across-bots verdict (compare.py-style aggregation over bots).
    sharpe_deltas = [
        b["verdict"].get("sharpe_delta") for b in by_bot.values() if b["verdict"].get("sharpe_delta") is not None
    ]
    dd_deltas = [
        b["verdict"].get("max_dd_delta_pct")
        for b in by_bot.values()
        if b["verdict"].get("max_dd_delta_pct") is not None
    ]
    wm_deltas = [
        b["verdict"].get("worst_month_delta_pct")
        for b in by_bot.values()
        if b["verdict"].get("worst_month_delta_pct") is not None
    ]
    median_across = {
        "n_bots": len(by_bot),
        "median_sharpe_delta": round(float(np.median(sharpe_deltas)), 4) if sharpe_deltas else None,
        "median_max_dd_delta_pct": round(float(np.median(dd_deltas)), 2) if dd_deltas else None,
        "median_worst_month_delta_pct": round(float(np.median(wm_deltas)), 2) if wm_deltas else None,
    }

    return {
        "n_trades_covered": int(len(covered)),
        "n_coins_covered": int(covered["symbol"].nunique()),
        "n_trades_sampled": int(len(sub)),
        "coverage_pct": round(100 * len(covered) / max(1, len(sub)), 1),
        "target_vol_ann_calibrated": round(target_vol, 2),
        "mult_summary": {
            "median": round(float(covered["mult"].median()), 3),
            "mean": round(float(covered["mult"].mean()), 3),
            "p10": round(float(covered["mult"].quantile(0.10)), 3),
            "p90": round(float(covered["mult"].quantile(0.90)), 3),
            "frac_at_floor": round(float((covered["mult"] <= MIN_SIZE + 1e-9).mean()), 3),
            "frac_at_cap": round(float((covered["mult"] >= MAX_LEVERAGE - 1e-9).mean()), 3),
        },
        "by_bot_leg": by_leg,
        "by_bot": by_bot,
        "pooled": pooled,
        "pooled_naive_target15": pooled_naive,
        "median_across_bots": median_across,
    }


def edge_scan(statement_timeout_ms: int = 40000) -> pd.DataFrame:
    """The empirical edge-discovery scan behind ``EDGE_MODELS``: realized WR and
    mean return per (model, direction) over ALL tags, real-geometry exits only,
    n >= 50. This is the reproducible evidence for AC1 ("identify edge-positive
    bots empirically") — run with ``--edge-scan``; the positive-mean legs of the
    named families are what ``EDGE_MODELS`` whitelists."""
    conn = _connect_readonly(statement_timeout_ms)
    try:
        sql = """
            with base as (
              select model, direction,
                case when direction = 'LONG' then (close_price - entry) / entry
                     else (entry - close_price) / entry end as r
              from closed_ai_signals
              where status not like 'LEGACY%%'
                and status <> all(%s)
                and entry is not null and close_price is not null
                and entry > 0 and close_price > 0
            )
            select model, direction, count(*) n,
              round(100 * avg((r > 0)::int)::numeric, 1) wr_pct,
              round(100 * avg(r)::numeric, 3) mean_pct,
              round(100 * stddev_samp(r)::numeric, 3) sd_pct
            from base group by model, direction
            having count(*) >= 50
            order by avg(r) desc
        """
        df = pd.read_sql(sql, conn, params=(list(_EXCLUDE_STATUS),))
    finally:
        conn.close()
    return df


def main() -> None:
    ap = argparse.ArgumentParser(description="GARCH vol-targeting live verdict (read-only study).")
    ap.add_argument("--limit-coins", type=int, default=None, help="sample the top-N coins by trade count")
    ap.add_argument("--statement-timeout-ms", type=int, default=20000)
    ap.add_argument("--json-out", help="write the full result JSON here")
    ap.add_argument(
        "--edge-scan", action="store_true", help="print the realized WR/mean edge scan (AC1 evidence) and exit"
    )
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.edge_scan:
        df = edge_scan(args.statement_timeout_ms)
        print(df.to_string(index=False))
        return

    result = run(args.limit_coins, args.statement_timeout_ms, verbose=not args.quiet)
    payload = json.dumps(result, indent=2, default=str)
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            fh.write(payload)
    print(payload)


if __name__ == "__main__":
    main()
