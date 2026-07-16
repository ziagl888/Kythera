#!/usr/bin/env py -3.13
# tools/skewness_study.py — K7 · SKW1 realized-skewness study (T-2026-CU-9050-141)
"""Read-only cross-section study for the shared realized-moments builder
(core.moment_features).

Question (docs/MODEL_CANDIDATES_SPEC_2026-07.md §K7): does realized **skewness**
(third moment of a coin's recent 15m log-returns) sort the cross-section of
forward weekly returns — specifically, is SHORT high-positive-skew vs LONG
low-skew a net-positive, funding- and fee-aware spread that is stable across a
chronological split? Realized-vol and kurtosis decile sorts are reported as a
byproduct (same machinery). This script does NOT decide deployment; it produces
the evidence so the later FULL run (Ein-Job-deferred) can.

⚠ FALLE (§K7, F6): this sorts on realized SKEWNESS, not on a MAX/lottery feature.
MAX-based shorts are contraindicated in crypto (the MAX effect inverts). The
builder emits Standard moment estimators only — no per-window max return.

Method (weekly rebalance, market-neutral long/short):
  * Universe = coins.json USDT-perps. One 15m-candle query per coin
    (core.moment_features.build_symbol_moment_panels + a close/volume side-load).
  * At each weekly timestamp t (Monday 00:00 UTC): realized moments as-of t
    (closed bars only, no lookahead), the forward 1-week return, a trailing-7d
    dollar-volume liquidity proxy, and the realized forward-week funding sum.
  * Market-neutral: each coin's forward return minus BTC's same-window return
    (BTC beta cancels in the long/short spread anyway; reported per-decile too).
  * Liquidity filter: within each week drop the bottom dollar-volume tercile.
  * Decile sort by the moment feature (rank-based, robust to small n in a smoke).
  * SKW1 spread: LONG bottom-skew decile, SHORT top-skew decile. Net =
    gross price spread + funding contribution (short earns funding, long pays it)
    − fees (both legs round-trip, tools.walkforward_sim.FEE_PER_SIDE).
  * Chronological val/test split of the weekly spread series — the sign must
    survive both halves (repo Rule 8: WR/gross alone is worthless).

This run is a **SMOKE** (--limit-symbols / --max-weeks caps): it proves the code
imports, the builder emits moments and the study runs end to end. The
full-universe report is deferred to the queue (Ein-Job-Regel — a second heavy
study must not run while another is live). The header of both artifacts says so.

READ-ONLY. SELECTs only, BELOW_NORMAL (tools.walkforward_sim.set_low_priority).
Artifacts to staging_models/ ONLY (repo Rule 2), never the repo root.

Contracts reused (no reinvention):
  * core.moment_features — the shared X-R1 as-of builder (15m log-returns →
    rolling std/skew/kurt over {24h, 7d}; native NaN, never fillna(0)).
  * core.funding_features.load_funding — the canonical funding loader; here we
    sum the RAW funding_rate (fraction, NOT bps) over the forward holding week
    as the funding PnL contribution.
  * tools.walkforward_sim.FEE_PER_SIDE = 0.0005 (taker 0.05%/side, P3.6) and
    set_low_priority / check_cpu_headroom — the fleet-safe guards used by the
    sibling studies (K3/K6).

Known bias (documented, not corrected): survivorship — coins.json / the per-coin
tables cover ACTIVE USDT-perps; delisted coins are partly missing, so every
weekly cross-section is over a survivorship-skewed universe. Funding for a coin
without funding history is treated as 0 contribution (documented, not imputed).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.candles import TF_SECONDS, read_candles  # noqa: E402
from core.database import db_connection  # noqa: E402
from core.funding_features import load_funding  # noqa: E402
from core.moment_features import (  # noqa: E402
    DEFAULT_TF,
    MOMENT_FEATURES,
    build_moment_panel,
    moment_features_asof,
)
from tools.walkforward_sim import FEE_PER_SIDE, check_cpu_headroom, set_low_priority  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(REPO_ROOT, "staging_models")

BTC_SYMBOL = "BTCUSDT"
WEEK = pd.Timedelta(days=7)
#: Trailing bars for the dollar-volume liquidity proxy (7d of 15m bars).
LIQ_BARS = 7 * 86400 // TF_SECONDS[DEFAULT_TF]
#: Fee drag charged on the L/S spread: 2 legs × round-trip (2 sides each).
LS_FEE_DRAG = 4 * FEE_PER_SIDE
#: Minimum coins in a week to attempt a decile sort (below → week skipped, noted).
MIN_COINS_PER_WEEK = 20
#: Sort features reported. Skewness is primary (the SKW1 hypothesis); the rest are
#: the §K7 byproduct (RV/kurtosis) plus the 24h-skew variant.
SORT_FEATURES = ["mom_skew_7d", "mom_skew_24h", "mom_rv_7d", "mom_kurt_7d"]
N_DECILES = 10


def load_coins(path: str = "coins.json") -> list[str]:
    with open(os.path.join(REPO_ROOT, path), encoding="utf-8") as fh:
        coins = json.load(fh)
    if not isinstance(coins, list) or not coins:
        raise ValueError(f"{path} is not a non-empty list")
    return coins


# ─────────────────────────────────────────────────────────────────────────────
# Per-coin side-load: closes/volumes for forward returns + liquidity + the panel
# ─────────────────────────────────────────────────────────────────────────────
class CoinSeries:
    """Holds one coin's 15m closes/volumes (numpy, tz-naive-UTC int64) + moment
    panel, with as-of helpers. One query per coin."""

    __slots__ = ("symbol", "ot", "close", "dollar", "panel")

    def __init__(self, symbol: str, df: pd.DataFrame, panel: pd.DataFrame):
        self.symbol = symbol
        ot = pd.to_datetime(df["open_time"], utc=True).dt.tz_localize(None)
        self.ot = ot.to_numpy().astype("int64")  # ns since epoch (UTC), ascending
        self.close = pd.to_numeric(df["close"], errors="coerce").to_numpy(dtype=float)
        vol = pd.to_numeric(df["volume"], errors="coerce").to_numpy(dtype=float)
        self.dollar = self.close * vol
        self.panel = panel

    def _asof_pos(self, ts: pd.Timestamp) -> int:
        """Index of the last bar that has CLOSED at/before ts (open_time+tf <= ts)."""
        cutoff = (ts - pd.Timedelta(seconds=TF_SECONDS[DEFAULT_TF])).value
        return int(np.searchsorted(self.ot, cutoff, side="right")) - 1

    def price_asof(self, ts: pd.Timestamp) -> float:
        pos = self._asof_pos(ts)
        return float(self.close[pos]) if pos >= 0 else np.nan

    def dollar_vol_trailing(self, ts: pd.Timestamp) -> float:
        pos = self._asof_pos(ts)
        if pos < LIQ_BARS - 1:
            return np.nan
        return float(np.nanmean(self.dollar[pos - LIQ_BARS + 1 : pos + 1]))


def load_coin_series(conn, symbols: list[str], start=None) -> dict[str, CoinSeries]:
    """One 15m query per coin → closes/volumes + moment panel. Coins without a
    table/data are skipped (survivorship), never fillna'd."""
    out: dict[str, CoinSeries] = {}
    for sym in symbols:
        try:
            df = read_candles(
                conn, sym, DEFAULT_TF, start=start, include_forming=False,
                columns=("open_time", "close", "volume"),
            )
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            continue
        if df.empty or "close" not in df.columns:
            continue
        df = df.sort_values("open_time").reset_index(drop=True)
        panel = build_moment_panel(df[["open_time", "close"]], tf=DEFAULT_TF)
        if panel.empty:
            continue
        out[sym] = CoinSeries(sym, df, panel)
    return out


def forward_funding_sum(fund_by_sym: dict, symbol: str, t: pd.Timestamp, t_next: pd.Timestamp) -> float:
    """Sum of RAW funding_rate (fraction) settled in (t, t_next]. A SHORT position
    earns positive funding; a LONG pays it. Symbols without funding history → 0.0
    contribution (documented, not imputed)."""
    g = fund_by_sym.get(symbol)
    if g is None or g.empty:
        return 0.0
    ft = g["funding_time"]
    m = (ft > t) & (ft <= t_next)
    if not m.any():
        return 0.0
    return float(pd.to_numeric(g.loc[m, "funding_rate"], errors="coerce").fillna(0.0).sum())


# ─────────────────────────────────────────────────────────────────────────────
# Weekly cross-section assembly
# ─────────────────────────────────────────────────────────────────────────────
def weekly_stamps(series: dict[str, CoinSeries], max_weeks: int | None) -> list[pd.Timestamp]:
    """Monday-00:00-UTC stamps spanning the data, leaving room for a trailing 7d
    moment window before and a forward 7d return after."""
    mins = [pd.Timestamp(s.ot[0], tz="UTC") for s in series.values() if len(s.ot)]
    maxs = [pd.Timestamp(s.ot[-1], tz="UTC") for s in series.values() if len(s.ot)]
    if not mins:
        return []
    lo = min(mins) + WEEK  # need trailing history for the moment window
    hi = max(maxs) - WEEK  # need a forward week for the return
    if hi <= lo:
        return []
    first_mon = (lo.normalize() + pd.Timedelta(days=(7 - lo.weekday()) % 7)).tz_convert("UTC")
    stamps = list(pd.date_range(first_mon, hi, freq="7D", tz="UTC"))
    if max_weeks is not None and len(stamps) > max_weeks:
        stamps = stamps[:max_weeks]
    return stamps


def build_weekly_rows(
    series: dict[str, CoinSeries], fund_by_sym: dict, stamps: list[pd.Timestamp]
) -> pd.DataFrame:
    """One row per (week, coin): as-of moments, market-neutral forward return,
    trailing dollar-volume, forward funding sum."""
    btc = series.get(BTC_SYMBOL)
    rows: list[dict] = []
    for t in stamps:
        t_next = t + WEEK
        btc_ret = np.nan
        if btc is not None:
            p0, p1 = btc.price_asof(t), btc.price_asof(t_next)
            if np.isfinite(p0) and np.isfinite(p1) and p0 > 0:
                btc_ret = p1 / p0 - 1.0
        for sym, s in series.items():
            if sym == BTC_SYMBOL:
                continue
            feats = moment_features_asof(s.panel, t, tf=DEFAULT_TF)
            if not feats or all(feats.get(f) is None for f in SORT_FEATURES):
                continue
            p0, p1 = s.price_asof(t), s.price_asof(t_next)
            if not (np.isfinite(p0) and np.isfinite(p1) and p0 > 0):
                continue
            fwd_ret = p1 / p0 - 1.0
            mn_ret = fwd_ret - btc_ret if np.isfinite(btc_ret) else np.nan
            row = {
                "week": t,
                "symbol": sym,
                "fwd_ret": fwd_ret,
                "mn_ret": mn_ret,
                "dollar_vol": s.dollar_vol_trailing(t),
                "fwd_funding_sum": forward_funding_sum(fund_by_sym, sym, t, t_next),
            }
            for f in SORT_FEATURES:
                row[f] = feats.get(f)
            rows.append(row)
    df = pd.DataFrame(rows)
    return df


def apply_liquidity_filter(df: pd.DataFrame) -> pd.DataFrame:
    """Drop the bottom dollar-volume tercile WITHIN each week."""
    if df.empty:
        return df
    keep = []
    for _, gm in df.groupby("week"):
        g = gm[gm["dollar_vol"].notna()]
        if len(g) < 3:
            keep.append(gm)  # too few to tercile — keep as-is (smoke)
            continue
        cut = float(np.quantile(g["dollar_vol"], 1 / 3))
        keep.append(g[g["dollar_vol"] >= cut])
    return pd.concat(keep).reset_index(drop=True) if keep else df


# ─────────────────────────────────────────────────────────────────────────────
# Decile sort + L/S spread
# ─────────────────────────────────────────────────────────────────────────────
def _assign_deciles(vals: np.ndarray) -> np.ndarray:
    """Rank-based decile 0..9 (robust to small n: some deciles stay empty)."""
    k = len(vals)
    ranks = pd.Series(vals).rank(method="first").to_numpy() - 1.0
    return np.minimum(N_DECILES - 1, (ranks / k * N_DECILES).astype(int))


def decile_table(df: pd.DataFrame, sort_col: str) -> dict:
    """Per-decile mean market-neutral forward return, deciles formed WITHIN each
    week then pooled. Weeks with < MIN_COINS_PER_WEEK usable coins are skipped."""
    per_decile: dict[int, list[float]] = {d: [] for d in range(N_DECILES)}
    n_weeks_used = 0
    for _, gm in df.groupby("week"):
        g = gm[gm[sort_col].notna() & gm["mn_ret"].notna()]
        if len(g) < MIN_COINS_PER_WEEK:
            continue
        n_weeks_used += 1
        dec = _assign_deciles(g[sort_col].to_numpy(dtype=float))
        for d, r in zip(dec, g["mn_ret"].to_numpy(dtype=float), strict=True):
            per_decile[int(d)].append(float(r))
    table = []
    for d in range(N_DECILES):
        vals = per_decile[d]
        table.append(
            {
                "decile": d,
                "n": len(vals),
                "avg_mn_ret": round(float(np.mean(vals)), 5) if vals else None,
                "wr": round(float(np.mean(np.array(vals) > 0)), 4) if vals else None,
            }
        )
    return {"sort_feature": sort_col, "n_weeks_used": n_weeks_used, "deciles": table}


def skew_ls_spread(df: pd.DataFrame, sort_col: str = "mom_skew_7d") -> dict:
    """SKW1: LONG bottom-skew decile, SHORT top-skew decile. Weekly net spread =
    gross price spread + funding contribution − fees; then chrono val/test."""
    weekly: list[dict] = []
    for t, gm in df.groupby("week"):
        g = gm[gm[sort_col].notna() & gm["mn_ret"].notna()]
        if len(g) < MIN_COINS_PER_WEEK:
            continue
        dec = _assign_deciles(g[sort_col].to_numpy(dtype=float))
        g = g.assign(_dec=dec)
        lo = g[g["_dec"] == 0]  # low skew → LONG
        hi = g[g["_dec"] == N_DECILES - 1]  # high positive skew → SHORT
        if lo.empty or hi.empty:
            continue
        gross = float(lo["mn_ret"].mean()) - float(hi["mn_ret"].mean())
        # Short earns funding on the high-skew leg, long pays it on the low-skew leg.
        funding = float(hi["fwd_funding_sum"].mean()) - float(lo["fwd_funding_sum"].mean())
        net = gross + funding - LS_FEE_DRAG
        weekly.append(
            {
                "week": t,
                "gross_spread": gross,
                "funding_contrib": funding,
                "net_spread": net,
                "n_long": int(len(lo)),
                "n_short": int(len(hi)),
            }
        )
    wf = pd.DataFrame(weekly)
    if wf.empty:
        return {"sort_feature": sort_col, "n_weeks": 0, "note": "no week reached MIN_COINS_PER_WEEK (smoke caps)"}

    def agg(sub: pd.DataFrame) -> dict:
        return {
            "n_weeks": int(len(sub)),
            "avg_gross_spread": round(float(sub["gross_spread"].mean()), 5),
            "avg_funding_contrib": round(float(sub["funding_contrib"].mean()), 6),
            "avg_net_spread": round(float(sub["net_spread"].mean()), 5),
            "pct_weeks_net_pos": round(float((sub["net_spread"] > 0).mean()), 4),
        }

    wf = wf.sort_values("week").reset_index(drop=True)
    cut = int(len(wf) * 0.7)
    return {
        "sort_feature": sort_col,
        "direction": "LONG low-skew decile, SHORT high-positive-skew decile",
        "fee_drag_per_week": round(LS_FEE_DRAG, 5),
        "all": agg(wf),
        "val_first70pct": agg(wf.iloc[:cut]) if cut >= 1 else None,
        "test_last30pct": agg(wf.iloc[cut:]) if len(wf) - cut >= 1 else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────────────────────
def build_markdown(meta: dict, spread: dict, deciles: list[dict]) -> str:
    L: list[str] = []
    L.append("# K7 · SKW1 — realized-skewness study (T-2026-CU-9050-141)\n")
    L.append("> **SMOKE — full run pending.** Produced with symbol/week caps to prove the builder +")
    L.append("> study run end to end. The full-universe report is deferred to the queue (Ein-Job-Regel:")
    L.append("> a second heavy study must not run while another is live).\n")
    L.append(f"_Generated {meta['generated_at']} · read-only · limit_symbols={meta['limit_symbols']} · ")
    L.append(f"max_weeks={meta['max_weeks']} · coins_loaded={meta['n_coins_loaded']} · weeks={meta['n_weeks']}_\n")
    L.append("Realized SKEWNESS sort (not MAX/lottery — §K7 F6). Market-neutral (coin − BTC), bottom")
    L.append("dollar-volume tercile dropped per week, funding on the short leg, taker fees both legs.")
    L.append("Survivorship-biased (active USDT-perps only).\n")

    L.append("## SKW1 long/short spread (LONG low-skew decile, SHORT high-positive-skew decile)\n")
    if spread.get("n_weeks") == 0 or "all" not in spread:
        L.append(f"- {spread.get('note', 'no spread computed')}\n")
    else:
        a = spread["all"]
        L.append(f"- sort feature: `{spread['sort_feature']}` · fee drag/week: {spread['fee_drag_per_week']}")
        L.append(
            f"- ALL ({a['n_weeks']} wk): gross {a['avg_gross_spread']} · funding {a['avg_funding_contrib']} · "
            f"**net {a['avg_net_spread']}** · weeks net+ {a['pct_weeks_net_pos']}"
        )
        for half in ("val_first70pct", "test_last30pct"):
            h = spread.get(half)
            if h:
                L.append(
                    f"- {half}: net {h['avg_net_spread']} ({h['n_weeks']} wk, weeks net+ {h['pct_weeks_net_pos']})"
                )
        L.append("")
        L.append("_Verdict (§K7 stop-criterion) is the FULL run's job: the net spread must be positive")
        L.append("AND survive both chrono halves. A smoke with capped weeks is not decisive._\n")

    L.append("## Decile sorts — mean market-neutral forward return (byproduct incl. RV/kurtosis)\n")
    for d in deciles:
        L.append(f"### `{d['sort_feature']}` ({d['n_weeks_used']} weeks ≥ {MIN_COINS_PER_WEEK} coins)\n")
        if d["n_weeks_used"] == 0:
            L.append("_no week reached the coin minimum (smoke caps) — deciles empty._\n")
            continue
        L.append("| decile | n | avg mn-ret | WR |")
        L.append("|--:|--:|--:|--:|")
        for row in d["deciles"]:
            L.append(f"| {row['decile']} | {row['n']} | {row['avg_mn_ret']} | {row['wr']} |")
        L.append("")

    L.append("## Caveats\n")
    L.append("- **SMOKE run**: caps make the numbers non-decisive; the stop-criterion verdict (§K7) is")
    L.append("  the FULL run's job. The moment feature-block stays a retrain option regardless (§K7).")
    L.append("- **Survivorship**: cross-section over active USDT-perps only; delisted coins missing.")
    L.append("- **Funding**: coins without funding history contribute 0 (documented, not imputed).")
    L.append("- Realized moments from 15m closed bars only (R1); native NaN, never fillna(0) (P1.20).")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="K7 · SKW1 realized-skewness study (read-only, SMOKE-capable).")
    ap.add_argument("--limit-symbols", type=int, default=None, help="Smoke: cap the universe to the first N coins.")
    ap.add_argument("--max-weeks", type=int, default=None, help="Smoke: cap the number of weekly rebalances.")
    ap.add_argument(
        "--start", default=None, help="Optional ISO date lower bound for the 15m candle load (e.g. 2025-10-01)."
    )
    ap.add_argument(
        "--skip-cpu-check",
        action="store_true",
        help="Skip the fleet CPU-headroom guard (ONLY for a deliberate tiny operator smoke under known load).",
    )
    args = ap.parse_args()

    set_low_priority()
    if not args.skip_cpu_check:
        check_cpu_headroom()
    os.makedirs(OUT_DIR, exist_ok=True)

    start = pd.Timestamp(args.start, tz="UTC") if args.start else None
    coins = load_coins()
    if args.limit_symbols:
        coins = coins[: args.limit_symbols]
    # BTC is always needed for market-neutral returns.
    if BTC_SYMBOL not in coins:
        coins = [BTC_SYMBOL] + coins
    print(f"universe: {len(coins)} coins (limit_symbols={args.limit_symbols})", flush=True)

    with db_connection() as conn:
        series = load_coin_series(conn, coins, start=start)
        print(f"loaded {len(series)} coin series (one 15m query per coin)", flush=True)
        fund_by_sym = load_funding(conn, [s for s in series if s != BTC_SYMBOL])

    stamps = weekly_stamps(series, args.max_weeks)
    print(f"weekly rebalances: {len(stamps)}", flush=True)

    rows = build_weekly_rows(series, fund_by_sym, stamps)
    print(f"weekly cross-section rows (pre-liquidity): {len(rows)}", flush=True)
    rows = apply_liquidity_filter(rows)
    print(f"rows after liquidity filter: {len(rows)}", flush=True)

    spread = skew_ls_spread(rows, "mom_skew_7d") if not rows.empty else {"n_weeks": 0, "note": "no rows"}
    deciles = [decile_table(rows, f) for f in SORT_FEATURES] if not rows.empty else []

    meta = {
        "study": "K7 · SKW1 (realized skewness / moments)",
        "task": "T-2026-CU-9050-141",
        "mode": "SMOKE — full run pending (Ein-Job deferred)",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "tf": DEFAULT_TF,
        "limit_symbols": args.limit_symbols,
        "max_weeks": args.max_weeks,
        "n_coins_loaded": len(series),
        "n_weeks": len(stamps),
        "n_rows_post_liquidity": int(len(rows)),
        "moment_features": MOMENT_FEATURES,
        "fee_per_side": FEE_PER_SIDE,
        "min_coins_per_week": MIN_COINS_PER_WEEK,
    }

    out = {"meta": meta, "skw1_spread": spread, "decile_sorts": deciles}
    json_path = os.path.join(OUT_DIR, "skewness_study.json")
    md_path = os.path.join(OUT_DIR, "skewness_study.md")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, default=str)
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(build_markdown(meta, spread, deciles))

    print("SMOKE — full run pending")
    print(f"coins={len(series)} weeks={len(stamps)} rows={len(rows)}")
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
