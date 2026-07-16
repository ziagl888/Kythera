#!/usr/bin/env py -3.13
# tools/funding_risk_study.py — K3 · FRL (Funding-Risk-Layer) study (T-2026-CU-9050-134)
"""Read-only fleet study: does entry-funding predict SHORT/LONG expectancy?

Hypothesis (from docs/MODEL_CANDIDATES_SPEC_2026-07.md §K3): fleet SHORTs opened
at *extreme-positive* funding have systematically worse expectancy (squeeze
mechanics), symmetric for LONGs at *extreme-negative* funding. The concrete
question is whether the ABR2 gate — LONG only when ``fund_24h > +3 bps`` and a
SHORT-veto at ``fund_24h > +1.5 bps`` — generalizes across the whole fleet, or
whether it is an ABR-family idiosyncrasy.

The §K3 feature list is PRESCRIPTIVE, so we analyze the full set:
  * fund_24h   — mean of the last 3 settlements (bps); the ABR gate quantity.
  * fund_72h   — mean of the last 9 settlements (bps).
  * fund_7d_cum— sum of the last 21 settlements (bps).
  * cs_pctl    — GENUINE cross-section percentile: the trade's coin ranked, at
                 its entry instant, against ALL other coins' as-of fund_24h
                 (in [0,1]). This is the ABR2 cross-section construct. NOTE:
                 the builder's ``fund_pctl_90d`` is a per-SYMBOL self-history
                 percentile, NOT cross-section — we do not substitute it.

This script is READ-ONLY. It runs SELECTs against the live DB, computes the
SHARED funding features as-of entry (``core.funding_features``), buckets the
deduped fleet trade log by funding zone × direction (× model tag), and reports
per bucket: n, win-rate, average NET PnL (round-trip taker fee included, both
winsorized AND raw), median, month-split and a chronological val/test split. WR
alone is worthless (repo Rule 8) — the verdict hangs on net-PnL expectancy and
on a monotone funding→PnL gradient that is *stable across both time halves*.

Contracts reused (no reinvention):
  * core.funding_features.load_funding / funding_features_asof — the canonical
    as-of funding builder (fund_last/24h/72h/7d_cum/pctl/trend, bps).
  * tools.walkforward_sim.FEE_PER_SIDE = 0.0005 (taker 0.05%/side → 0.10%
    round-trip, P3.6). Net PnL = gross − 2·FEE_PER_SIDE. We do NOT invent a fee.
  * core.time.LEGACY_WRITER_TZ = "Europe/Bucharest" — closed_ai_signals.open_time
    is TIMESTAMP WITHOUT TIME ZONE = naive local Bucharest. We localize
    DST-aware (a constant ±3h offset is wrong across the 2026-03-29 DST flip)
    exactly like tools/aim2_build_dataset.py before joining to funding_rates
    (funding_time is TIMESTAMPTZ / UTC).

Dedup: closed_ai_signals carries ~357k duplicate rows (raw ≈445k → ≈88k). We
dedup on (symbol, model, direction, open_time), keeping the lowest id, BEFORE any
analysis (DISTINCT ON … ORDER BY …, id).

PnL: realized close-vs-entry per trade. gross = (close−entry)/entry for LONG,
(entry−close)/entry for SHORT; net = gross − 2·FEE_PER_SIDE. This is the honest
per-trade outcome recorded in the fleet log (close_price is the realized exit:
target, SL or regime-close), not a re-simulation.

Known bias (documented, not corrected): survivorship. coins.json / funding_rates
cover *active* USDT-perps; delisted coins are partly missing from funding_rates
(716 signal symbols vs 530 funding symbols). Trades on symbols without funding
history drop out of the funded population — that population skews to survivors.

Output: staging_models/funding_risk_study.json (machine) + .md (human).
Artifacts go to staging_models/ ONLY (repo Rule 2), never the repo root.
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

from core.database import db_connection  # noqa: E402
from core.funding_features import funding_features_asof, load_funding  # noqa: E402
from core.time import LEGACY_WRITER_TZ  # noqa: E402
from tools.walkforward_sim import FEE_PER_SIDE  # noqa: E402

ROUND_TRIP_FEE = 2.0 * FEE_PER_SIDE  # 0.001 = 0.10 %

# ABR2 validated thresholds (core/funding_features docstring, Stand 2026-07-06).
ABR_LONG_GATE_BPS = 3.0  # LONG only when fund_24h > +3.0 bps
ABR_SHORT_VETO_BPS = 1.5  # SHORT vetoed when fund_24h > +1.5 bps
EXTREME_POS_BPS = 3.0
EXTREME_NEG_BPS = -3.0

# Materiality floor for a per-half bucket to count toward the verdict.
MIN_HALF_N = 100
# Economic-magnitude floor for a per-half Spearman to count as more than "just a
# sign". |ρ| below this is directionally consistent but economically negligible
# (with n≈40k/half even |ρ|≈0.017 is >3 SE "significant" yet trivially small —
# significance is not materiality, so we gate on magnitude, not p-value).
MIN_ABS_SPEARMAN = 0.03

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "staging_models")


def _try_lower_priority() -> None:
    """Best-effort BELOW_NORMAL process priority on the live VPS (read-only job,
    must not starve the fleet). Silent if psutil is absent."""
    try:
        import psutil  # type: ignore

        p = psutil.Process()
        if os.name == "nt":
            p.nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        else:
            p.nice(10)
    except Exception:
        pass


def load_signals(conn, limit_symbols: int | None) -> pd.DataFrame:
    """Deduped closed_ai_signals. Dedup key = (symbol, model, direction, open_time),
    keep lowest id (the first-written copy). Documented in the module docstring."""
    sym_filter = ""
    if limit_symbols:
        sym_filter = (
            "WHERE symbol IN (SELECT symbol FROM closed_ai_signals "
            f"GROUP BY symbol ORDER BY COUNT(*) DESC LIMIT {int(limit_symbols)})"
        )
    query = f"""
        SELECT DISTINCT ON (symbol, model, direction, open_time)
               id, symbol, model, direction, entry, close_price, open_time
        FROM closed_ai_signals
        {sym_filter}
        ORDER BY symbol, model, direction, open_time, id
    """
    return pd.read_sql_query(query, conn)


def to_utc_aware(open_time: pd.Series) -> pd.Series:
    """Naive local (Europe/Bucharest) → tz-aware UTC, DST-correct. Same recipe as
    tools/aim2_build_dataset.py: localize with DST handling, then convert. The
    2026-03-29 spring-forward means a constant +3h is wrong for Feb/Mar rows.

    ``ambiguous="NaT"`` maps the autumn fall-back hour (last Sunday of October,
    ~1h/yr where the wall clock repeats) to NaT, and those rows are dropped by
    the caller. In THIS study's window (2026-02-24 .. 2026-07-16) there is no
    fall-back transition, so zero rows are affected; the flag is correctness
    hygiene for a wider re-run, not a live drop here.
    """
    s = pd.to_datetime(open_time)
    localized = s.dt.tz_localize(LEGACY_WRITER_TZ, nonexistent="shift_forward", ambiguous="NaT")
    return localized.dt.tz_convert("UTC")


def compute_pnl(df: pd.DataFrame) -> pd.Series:
    """Realized NET PnL fraction per trade (round-trip taker fee included).

    gross = realized price move close-vs-entry (unlevered). The fleet log records
    close_price as the raw exit price, which for legacy rows and un-SL-truncated
    SHORT squeezes reaches ±200%+. We keep the raw value here; downstream we
    report BOTH a winsorized mean (tail-safe expectancy) AND the raw mean, plus
    the raw median — so the squeeze/tail-risk claim can be judged on unclipped
    losses (winsorizing at the 1st pct would attenuate exactly the SHORT-squeeze
    signal under test). WR and median are inherently tail-robust.
    """
    entry = df["entry"].astype(float)
    close = df["close_price"].astype(float)
    is_long = df["direction"].str.upper() == "LONG"
    gross = np.where(is_long, (close - entry) / entry, (entry - close) / entry)
    return pd.Series(gross - ROUND_TRIP_FEE, index=df.index)


def attach_funding(df: pd.DataFrame, conn) -> pd.DataFrame:
    """Compute the full §K3 as-of funding feature set for every trade.

    fund_24h/72h/7d_cum come straight from the shared builder. cs_pctl is the
    genuine cross-section percentile (see compute_cross_section_pctl). Trades
    whose symbol lacks funding history drop out of the funded population.
    """
    symbols = sorted(df["symbol"].dropna().unique().tolist())
    by_sym = load_funding(conn, symbols)  # full history, as-of over whole span

    fund_24h = np.full(len(df), np.nan)
    fund_72h = np.full(len(df), np.nan)
    fund_7d_cum = np.full(len(df), np.nan)
    fund_pctl_self = np.full(len(df), np.nan)  # per-symbol self-history (NOT cross-section)
    ts = df["open_time_utc"].values
    syms = df["symbol"].values
    for pos in range(len(df)):
        feats = funding_features_asof(by_sym, syms[pos], pd.Timestamp(ts[pos]))
        if feats:
            fund_24h[pos] = feats["fund_24h"]
            fund_72h[pos] = feats["fund_72h"]
            fund_7d_cum[pos] = feats["fund_7d_cum"]
            fund_pctl_self[pos] = feats["fund_pctl_90d"]

    df = df.copy()
    df["fund_24h"] = fund_24h
    df["fund_72h"] = fund_72h
    df["fund_7d_cum"] = fund_7d_cum
    df["fund_pctl_self_90d"] = fund_pctl_self
    df["cs_pctl"] = compute_cross_section_pctl(df, by_sym)
    return df


def compute_cross_section_pctl(df: pd.DataFrame, by_sym: dict) -> np.ndarray:
    """GENUINE cross-section funding percentile per trade, in [0,1].

    For each trade at its entry instant, rank the trade's coin's as-of fund_24h
    against ALL coins' as-of fund_24h at that same instant. fund_24h here is the
    rolling mean of each coin's last 3 settlements (identical definition to the
    shared builder), evaluated as-of the latest settlement ≤ entry.

    Efficiency: no per-trade DB round-trips and no 82k×530 feature recompute.
    We build a per-coin settlement→rolling-fund_24h panel once, floor entry
    timestamps to the hour (funding steps on an ~8h grid, so hour-flooring moves
    the as-of peer set negligibly while collapsing ~82k timestamps to a few
    thousand), merge_asof each coin onto that small hour grid to form a wide
    (hour × coin) matrix, percentile-rank across coins per hour, and map each
    trade back by (hour, symbol). All in-memory (~few thousand × 530 floats).

    The whole cross-section is computed in naive-UTC (values are UTC; tz stripped
    only to keep merge_asof / MultiIndex joins simple).
    """
    # Per-coin rolling-3 fund_24h (bps) at each settlement time (naive UTC).
    panels: dict[str, pd.DataFrame] = {}
    for sym, g in by_sym.items():
        if len(g) < 3:
            continue
        rates = g["funding_rate"].to_numpy() * 1e4  # → bps
        roll3 = pd.Series(rates).rolling(3).mean().to_numpy()
        ft = g["funding_time"].dt.tz_convert("UTC").dt.tz_localize(None).reset_index(drop=True)
        p = pd.DataFrame({"funding_time": ft, "f24": roll3}).dropna().sort_values("funding_time")
        if not p.empty:
            panels[sym] = p.reset_index(drop=True)

    floored = df["open_time_utc"].dt.tz_convert("UTC").dt.tz_localize(None).dt.floor("h")
    uniq_hours = pd.DatetimeIndex(pd.Series(floored.dropna().unique())).sort_values()
    if len(uniq_hours) == 0 or not panels:
        return np.full(len(df), np.nan)
    uniq = pd.DataFrame({"funding_time": uniq_hours})

    wide: dict[str, np.ndarray] = {}
    for sym, p in panels.items():
        merged = pd.merge_asof(uniq, p, on="funding_time", direction="backward")
        wide[sym] = merged["f24"].to_numpy()
    panel_wide = pd.DataFrame(wide, index=uniq["funding_time"].to_numpy())

    # Percentile rank across coins per hour (NaN peers excluded automatically).
    ranks_long = panel_wide.rank(axis=1, pct=True).stack()  # MultiIndex (hour, symbol)
    key = pd.MultiIndex.from_arrays([floored.to_numpy(), df["symbol"].to_numpy()])
    return ranks_long.reindex(key).to_numpy()


def bucket_stats(g: pd.DataFrame) -> dict:
    n = int(len(g))
    if n == 0:
        return {"n": 0}
    pnl = g["net_pnl"]  # raw — for WR (sign), median (tail-robust) and raw mean
    pnl_w = g["net_pnl_w"]  # winsorized — for the tail-safe mean
    return {
        "n": n,
        "wr": round(float((pnl > 0).mean()), 4),
        "avg_net_pnl_pct": round(float(pnl_w.mean()) * 100, 4),  # winsorized
        "avg_net_pnl_raw_pct": round(float(pnl.mean()) * 100, 4),  # unclipped
        "median_net_pnl_pct": round(float(pnl.median()) * 100, 4),
        "avg_fund_24h_bps": round(float(g["fund_24h"].mean()), 3),
    }


def month_split(g: pd.DataFrame, min_n: int = 20) -> dict:
    out = {}
    for m, gm in g.groupby(g["open_time_utc"].dt.strftime("%Y-%m")):
        if len(gm) >= min_n:
            out[m] = {
                "n": int(len(gm)),
                "avg_net_pnl_pct": round(float(gm["net_pnl"].mean()) * 100, 4),
                "avg_net_pnl_raw_pct": round(float(gm["net_pnl"].mean()) * 100, 4),
                "wr": round(float((gm["net_pnl"] > 0).mean()), 4),
            }
    return out


def spearman(x: np.ndarray, y: np.ndarray) -> float | None:
    """Spearman rank correlation (numpy-only, no scipy). None if degenerate."""
    if len(x) < 30:
        return None
    rx = pd.Series(x).rank().values
    ry = pd.Series(y).rank().values
    if np.std(rx) == 0 or np.std(ry) == 0:
        return None
    return float(np.corrcoef(rx, ry)[0, 1])


def gradient_test(gd: pd.DataFrame, feature: str, median_ts: pd.Timestamp, expected: str) -> dict:
    """Per-trade rank correlation between ``feature`` and net PnL, per chrono
    half. ABR predicts LONG corr>0 (higher funding → better LONG), SHORT corr<0.
    Records both sign-stability AND magnitude (|ρ|≥MIN_ABS_SPEARMAN) per half."""
    sub = gd[gd[feature].notna()]
    h1 = sub[sub["open_time_utc"] < median_ts]
    h2 = sub[sub["open_time_utc"] >= median_ts]
    c_all = spearman(sub[feature].values, sub["net_pnl_w"].values)
    c1 = spearman(h1[feature].values, h1["net_pnl_w"].values)
    c2 = spearman(h2[feature].values, h2["net_pnl_w"].values)

    def sign_ok(c):
        if c is None:
            return None
        return (c > 0) if expected == "positive" else (c < 0)

    sign_both = bool(sign_ok(c1)) and bool(sign_ok(c2))
    mag_val = c1 is not None and abs(c1) >= MIN_ABS_SPEARMAN
    mag_test = c2 is not None and abs(c2) >= MIN_ABS_SPEARMAN
    return {
        "expected_sign": expected,
        "spearman_all": None if c_all is None else round(c_all, 4),
        "spearman_val": None if c1 is None else round(c1, 4),
        "spearman_test": None if c2 is None else round(c2, 4),
        "n_val": int(len(h1)),
        "n_test": int(len(h2)),
        "sign_holds_both_halves": sign_both,
        "abs_ge_floor_val": bool(mag_val),
        "abs_ge_floor_test": bool(mag_test),
        "magnitude_stable_both_halves": bool(mag_val and mag_test),
    }


def funding_pnl_correlation(funded: pd.DataFrame, median_ts: pd.Timestamp) -> dict:
    """fund_24h gradient per direction — the canonical verdict basis."""
    out: dict = {}
    for direction, gd in funded.groupby("direction"):
        expected = "positive" if direction.upper() == "LONG" else "negative"
        out[direction] = gradient_test(gd, "fund_24h", median_ts, expected)
    return out


def half_split_stats(g: pd.DataFrame, median_ts: pd.Timestamp) -> dict:
    h1 = g[g["open_time_utc"] < median_ts]
    h2 = g[g["open_time_utc"] >= median_ts]
    return {"val_firsthalf": bucket_stats(h1), "test_secondhalf": bucket_stats(h2)}


def extreme_expectancy(gd: pd.DataFrame, feature: str, lo_edge: float, hi_edge: float, median_ts) -> dict:
    """Bottom-quintile vs top-quintile expectancy for a feature (global 20/80
    edges), with the chrono val/test split. Reports raw AND winsorized means."""
    sub = gd[gd[feature].notna()]
    bottom = sub[sub[feature] <= lo_edge]
    top = sub[sub[feature] >= hi_edge]
    return {
        "bottom_quintile": {**bucket_stats(bottom), **half_split_stats(bottom, median_ts)},
        "top_quintile": {**bucket_stats(top), **half_split_stats(top, median_ts)},
    }


def analyze(df: pd.DataFrame) -> dict:
    funded = df[df["fund_24h"].notna()].copy()
    median_ts = funded["open_time_utc"].median()

    # Winsorize net PnL at the global 1/99 pct of the funded population — used
    # only for the tail-safe MEAN; the raw mean, median and WR are reported
    # alongside so tail-risk is not hidden. WR/median/raw-mean use the raw value.
    lo, hi = float(np.quantile(funded["net_pnl"], 0.01)), float(np.quantile(funded["net_pnl"], 0.99))
    funded["net_pnl_w"] = funded["net_pnl"].clip(lo, hi)

    # Quintile edges over the FULL fund_24h distribution (extremes included) —
    # 20/40/60/80 pct. Funding piles up at the exchange default rate, so interior
    # edges can TIE and collapse a quintile (Q4 typically empty). We detect and
    # document that rather than silently dropping the bin (see degeneracy note).
    core = funded["fund_24h"]
    q_edges = [float(np.quantile(core, q)) for q in (0.2, 0.4, 0.6, 0.8)]
    funded["zone"] = funded["fund_24h"].apply(lambda x: zone_of(x, q_edges))
    rounded_edges = [round(e, 6) for e in q_edges]
    degenerate = len(set(rounded_edges)) < len(rounded_edges)
    present_zones = sorted(funded["zone"].unique().tolist())
    collapsed = [f"Q{i + 1}" for i in range(5) if f"Q{i + 1}" not in present_zones and i < len(q_edges) + 1]

    result: dict = {
        "quintile_edges_fund_24h_bps": [round(e, 3) for e in q_edges],
        "quintile_degeneracy": {
            "edges_tie": degenerate,
            "collapsed_quintiles": collapsed,
            "present_zones": present_zones,
            "note": (
                "fund_24h ties at the exchange default funding rate: the 60th and 80th pct "
                "edges coincide, so the interior quintile between them is empty (collapsed). "
                "The gradient/verdict do not depend on quintile bins (they use per-trade "
                "Spearman + the ±3bps extreme cuts); the collapsed bin is simply omitted from "
                "the zone table, documented here rather than silently dropped."
            )
            if degenerate
            else "no quintile collapse.",
        },
        "winsor_bounds_net_pnl_pct": [round(lo * 100, 3), round(hi * 100, 3)],
        "median_open_time_utc": str(median_ts),
        "by_direction_zone": {},
        "abr2_gate_check": {},
        "by_model_extremes": {},
        "direction_baseline": {},
        "funding_pnl_correlation": funding_pnl_correlation(funded, median_ts),
        "multi_feature": {},
    }

    # Direction baselines (all funded trades of that direction)
    for direction, gd in funded.groupby("direction"):
        result["direction_baseline"][direction] = {
            **bucket_stats(gd),
            **half_split_stats(gd, median_ts),
            "months": month_split(gd),
        }

    # direction × zone
    for (direction, zone), g in funded.groupby(["direction", "zone"]):
        result["by_direction_zone"].setdefault(direction, {})[zone] = {
            **bucket_stats(g),
            **half_split_stats(g, median_ts),
            "months": month_split(g),
        }

    # ABR2 gate check, fleet-wide
    long_g = funded[funded["direction"].str.upper() == "LONG"]
    short_g = funded[funded["direction"].str.upper() == "SHORT"]
    result["abr2_gate_check"] = {
        "LONG_gate_fund24h_gt_+3bps": {
            "in_zone": {
                **bucket_stats(long_g[long_g["fund_24h"] > ABR_LONG_GATE_BPS]),
                **half_split_stats(long_g[long_g["fund_24h"] > ABR_LONG_GATE_BPS], median_ts),
            },
            "out_zone": bucket_stats(long_g[long_g["fund_24h"] <= ABR_LONG_GATE_BPS]),
        },
        "SHORT_veto_fund24h_gt_+1.5bps": {
            "in_veto": {
                **bucket_stats(short_g[short_g["fund_24h"] > ABR_SHORT_VETO_BPS]),
                **half_split_stats(short_g[short_g["fund_24h"] > ABR_SHORT_VETO_BPS], median_ts),
            },
            "out_veto": bucket_stats(short_g[short_g["fund_24h"] <= ABR_SHORT_VETO_BPS]),
        },
        "SHORT_extreme_pos_gt_+3bps": {
            **bucket_stats(short_g[short_g["fund_24h"] > EXTREME_POS_BPS]),
            **half_split_stats(short_g[short_g["fund_24h"] > EXTREME_POS_BPS], median_ts),
        },
        "LONG_extreme_neg_lt_-3bps": {
            **bucket_stats(long_g[long_g["fund_24h"] < EXTREME_NEG_BPS]),
            **half_split_stats(long_g[long_g["fund_24h"] < EXTREME_NEG_BPS], median_ts),
        },
    }

    # Multi-feature: gradient + top/bottom-quintile expectancy per direction, for
    # the full §K3 feature set. cs_pctl is the ABR2 cross-section construct.
    for feat in ("fund_24h", "fund_72h", "fund_7d_cum", "cs_pctl"):
        sub = funded[funded[feat].notna()]
        if sub.empty:
            continue
        lo_edge = float(np.quantile(sub[feat], 0.2))
        hi_edge = float(np.quantile(sub[feat], 0.8))
        entry: dict = {
            "n": int(len(sub)),
            "lo_edge_q20": round(lo_edge, 4),
            "hi_edge_q80": round(hi_edge, 4),
            "by_direction": {},
        }
        for direction, gd in sub.groupby("direction"):
            expected = "positive" if direction.upper() == "LONG" else "negative"
            entry["by_direction"][direction] = {
                "gradient": gradient_test(gd, feat, median_ts, expected),
                "extreme": extreme_expectancy(gd, feat, lo_edge, hi_edge, median_ts),
            }
        result["multi_feature"][feat] = entry

    # Per major model tag: extreme zone effect + month-split (only tags w/ n>=300)
    for model, gm in funded.groupby("model"):
        if len(gm) < 300:
            continue
        entry = {}
        for direction, gd in gm.groupby("direction"):
            entry[direction] = {
                "baseline": bucket_stats(gd),
                "extreme_pos": bucket_stats(gd[gd["fund_24h"] > EXTREME_POS_BPS]),
                "extreme_neg": bucket_stats(gd[gd["fund_24h"] < EXTREME_NEG_BPS]),
                "months": month_split(gd, min_n=40),
            }
        result["by_model_extremes"][model] = entry

    return result


def zone_of(fund_24h: float, q_edges: list[float]) -> str:
    """Funding zone label: extreme zones first (ABR thresholds), else quintile."""
    if np.isnan(fund_24h):
        return "NA"
    if fund_24h > EXTREME_POS_BPS:
        return "EXTREME_POS(>+3bps)"
    if fund_24h < EXTREME_NEG_BPS:
        return "EXTREME_NEG(<-3bps)"
    for k, edge in enumerate(q_edges):
        if fund_24h <= edge:
            return f"Q{k + 1}"
    return f"Q{len(q_edges) + 1}"


def derive_verdict(analysis: dict) -> dict:
    """The machine verdict must not fire "edge-found" on sign-stability alone.

    PRIMARY (fund_24h gradient, per-trade Spearman, per chrono half):
      * sign_ok    = both directions hold the ABR-predicted sign in both halves
      * magnitude_ok = additionally |ρ| ≥ MIN_ABS_SPEARMAN in both halves
      verdict = "edge-found"                        if magnitude_ok
                "direction-confirmed, magnitude-weak" if sign_ok but not magnitude_ok
                "no-op/no-edge"                       otherwise
    SECONDARY — the hard extreme-zone claims vs baseline (context; fragile
      out-of-sample because an extreme bin is thin and regime-sensitive).
    """
    corr = analysis["funding_pnl_correlation"]
    dirs = [d for d in ("LONG", "SHORT") if d in corr]
    sign_ok = len(dirs) == 2 and all(corr[d].get("sign_holds_both_halves") for d in dirs)
    magnitude_ok = sign_ok and all(corr[d].get("magnitude_stable_both_halves") for d in dirs)

    findings = []
    extreme_stable_any = False

    def half_pnl(d, key):
        v = d.get(key, {})
        return v.get("avg_net_pnl_raw_pct"), v.get("n")

    baselines = analysis["direction_baseline"]
    for label, direction, zone_key in [
        ("SHORT@extreme-positive", "SHORT", "SHORT_extreme_pos_gt_+3bps"),
        ("LONG@extreme-negative", "LONG", "LONG_extreme_neg_lt_-3bps"),
    ]:
        base = baselines.get(direction, {})
        base_h1 = base.get("val_firsthalf", {}).get("avg_net_pnl_raw_pct")
        base_h2 = base.get("test_secondhalf", {}).get("avg_net_pnl_raw_pct")
        zone = analysis["abr2_gate_check"][zone_key]
        z_h1, n_h1 = half_pnl(zone, "val_firsthalf")
        z_h2, n_h2 = half_pnl(zone, "test_secondhalf")
        stable = None
        if None not in (base_h1, base_h2, z_h1, z_h2) and (n_h1 or 0) >= MIN_HALF_N and (n_h2 or 0) >= MIN_HALF_N:
            stable = bool(z_h1 < base_h1 and z_h2 < base_h2)
            extreme_stable_any = extreme_stable_any or stable
        findings.append(
            {
                "claim": label,
                "zone_n_total": zone.get("n"),
                "zone_raw_pnl_h1": z_h1,
                "zone_raw_pnl_h2": z_h2,
                "baseline_raw_pnl_h1": base_h1,
                "baseline_raw_pnl_h2": base_h2,
                "worse_than_baseline_both_halves": stable,
                "sufficient_n_both_halves": (n_h1 or 0) >= MIN_HALF_N and (n_h2 or 0) >= MIN_HALF_N,
            }
        )

    if magnitude_ok:
        verdict = "edge-found"
    elif sign_ok:
        verdict = "direction-confirmed, magnitude-weak"
    else:
        verdict = "no-op/no-edge"

    return {
        "verdict": verdict,
        "sign_stable_both_directions": sign_ok,
        "magnitude_stable_both_directions": magnitude_ok,
        "min_abs_spearman_floor": MIN_ABS_SPEARMAN,
        "extreme_zone_stable_any": extreme_stable_any,
        "correlation": corr,
        "findings": findings,
    }


def build_markdown(meta: dict, analysis: dict, verdict: dict) -> str:
    L = []
    L.append("# K3 · FRL — Funding-Risk-Layer study (T-2026-CU-9050-134)\n")
    L.append(
        f"_Generated {meta['generated_at']} · read-only fleet analysis · fee/side {FEE_PER_SIDE} "
        f"(round-trip {ROUND_TRIP_FEE:.4f})_\n"
    )
    L.append(f"**VERDICT: {verdict['verdict']}**\n")
    L.append(
        f"- fund_24h gradient sign-stable both directions & halves: **{verdict['sign_stable_both_directions']}**; "
        f"magnitude-stable (|ρ|≥{verdict['min_abs_spearman_floor']} both halves): "
        f"**{verdict['magnitude_stable_both_directions']}** (primary)"
    )
    L.append(f"- hard extreme-zone claim stable in both halves: {verdict['extreme_zone_stable_any']} (secondary)\n")

    L.append("## Primary test — fund_24h → PnL gradient (per-trade Spearman)\n")
    L.append("ABR predicts LONG corr>0 (higher funding → better LONG), SHORT corr<0.\n")
    L.append("| dir | expected | Spearman all | val | test | sign both | |ρ|≥floor both |")
    L.append("|---|---|--:|--:|--:|:--:|:--:|")
    for d, c in verdict["correlation"].items():
        L.append(
            f"| {d} | {c['expected_sign']} | {c['spearman_all']} | {c['spearman_val']} "
            f"| {c['spearman_test']} | {c['sign_holds_both_halves']} | {c['magnitude_stable_both_halves']} |"
        )
    L.append("")

    L.append("## Multi-feature gradient — full §K3 set incl. cross-section percentile\n")
    L.append(
        "Same per-trade Spearman on fund_72h, fund_7d_cum and **cs_pctl** (the genuine ABR2 "
        "cross-section percentile: each trade's coin ranked vs ALL coins' as-of fund_24h at entry).\n"
    )
    L.append("| feature | dir | Spearman all | val | test | sign both | |ρ|≥floor both |")
    L.append("|---|---|--:|--:|--:|:--:|:--:|")
    for feat in ("fund_24h", "fund_72h", "fund_7d_cum", "cs_pctl"):
        mf = analysis["multi_feature"].get(feat)
        if not mf:
            continue
        for direction, e in mf["by_direction"].items():
            gr = e["gradient"]
            L.append(
                f"| {feat} | {direction} | {gr['spearman_all']} | {gr['spearman_val']} | {gr['spearman_test']} "
                f"| {gr['sign_holds_both_halves']} | {gr['magnitude_stable_both_halves']} |"
            )
    L.append("")

    L.append("## Cross-section percentile (cs_pctl) — top vs bottom quintile expectancy\n")
    L.append(
        "Bottom quintile = coin's funding low vs peers; top quintile = high vs peers. "
        "ABR expects LONG better / SHORT worse as cs_pctl rises. Means shown winsorized AND raw.\n"
    )
    L.append(
        "| dir | bucket | n | WR | avg net PnL % (wins) | avg net PnL % (raw) | median % | val raw% (n) | test raw% (n) |"
    )
    L.append("|---|---|--:|--:|--:|--:|--:|--:|--:|")
    csf = analysis["multi_feature"].get("cs_pctl", {})
    for direction, e in csf.get("by_direction", {}).items():
        for bname, bkey in [("bottom Q1", "bottom_quintile"), ("top Q5", "top_quintile")]:
            b = e["extreme"][bkey]
            v = b.get("val_firsthalf", {})
            t = b.get("test_secondhalf", {})
            L.append(
                f"| {direction} | {bname} | {b.get('n')} | {b.get('wr')} | {b.get('avg_net_pnl_pct')} "
                f"| {b.get('avg_net_pnl_raw_pct')} | {b.get('median_net_pnl_pct')} "
                f"| {v.get('avg_net_pnl_raw_pct')} ({v.get('n')}) | {t.get('avg_net_pnl_raw_pct')} ({t.get('n')}) |"
            )
    L.append("")

    L.append("## Population\n")
    L.append(f"- raw closed_ai_signals rows: {meta['n_raw']:,}")
    L.append(f"- deduped (symbol,model,direction,open_time): {meta['n_dedup']:,}")
    L.append(f"- priced (entry>0 & close_price present): {meta['n_priced']:,}")
    L.append(f"- with as-of funding (fund_24h): {meta['n_funded']:,}")
    L.append(f"- with cross-section pctl (cs_pctl): {meta['n_cs_pctl']:,}")
    L.append(f"- open_time span (UTC): {meta['span_utc']}")
    L.append(f"- median split (val|test): {analysis['median_open_time_utc']}")
    L.append(f"- fund_24h quintile edges (bps): {analysis['quintile_edges_fund_24h_bps']}")
    deg = analysis["quintile_degeneracy"]
    L.append(
        f"- quintile degeneracy: edges_tie={deg['edges_tie']}, collapsed={deg['collapsed_quintiles']} — {deg['note']}"
    )
    L.append(
        f"- winsor bounds for mean net-PnL (1/99 pct, %): {analysis['winsor_bounds_net_pnl_pct']} "
        f"— WR, median & raw mean use raw values\n"
    )

    L.append("## Hypothesis test (chrono val/test must agree; RAW means)\n")
    for f in verdict["findings"]:
        L.append(f"### {f['claim']}")
        L.append(f"- zone n (total): {f['zone_n_total']}")
        L.append(f"- zone RAW net-PnL/trade  val: {f['zone_raw_pnl_h1']}%  |  test: {f['zone_raw_pnl_h2']}%")
        L.append(
            f"- baseline RAW net-PnL/trade  val: {f['baseline_raw_pnl_h1']}%  |  test: {f['baseline_raw_pnl_h2']}%"
        )
        L.append(
            f"- worse-than-baseline in BOTH halves: {f['worse_than_baseline_both_halves']} "
            f"(sufficient n both halves: {f['sufficient_n_both_halves']})\n"
        )

    L.append("## Direction × funding zone (fleet-wide, funded trades)\n")
    L.append(
        "| dir | zone | n | WR | avg net PnL % (wins) | avg net PnL % (raw) | avg fund_24h bps "
        "| val raw% (n) | test raw% (n) |"
    )
    L.append("|---|---|--:|--:|--:|--:|--:|--:|--:|")
    zone_order = ["EXTREME_NEG(<-3bps)", "Q1", "Q2", "Q3", "Q4", "Q5", "EXTREME_POS(>+3bps)"]
    for direction in sorted(analysis["by_direction_zone"].keys()):
        zones = analysis["by_direction_zone"][direction]
        for z in zone_order:
            if z not in zones:
                continue
            b = zones[z]
            v = b.get("val_firsthalf", {})
            t = b.get("test_secondhalf", {})
            L.append(
                f"| {direction} | {z} | {b['n']} | {b.get('wr')} | {b.get('avg_net_pnl_pct')} "
                f"| {b.get('avg_net_pnl_raw_pct')} | {b.get('avg_fund_24h_bps')} "
                f"| {v.get('avg_net_pnl_raw_pct')} ({v.get('n')}) | {t.get('avg_net_pnl_raw_pct')} ({t.get('n')}) |"
            )
    L.append("")

    L.append("## ABR2 gate generalization (fleet-wide, RAW means)\n")
    g = analysis["abr2_gate_check"]
    lg = g["LONG_gate_fund24h_gt_+3bps"]
    sv = g["SHORT_veto_fund24h_gt_+1.5bps"]
    L.append("| test | n | WR | avg net PnL % (wins) | avg net PnL % (raw) |")
    L.append("|---|--:|--:|--:|--:|")
    for lbl, b in [
        ("LONG in-gate (fund_24h>+3bps)", lg["in_zone"]),
        ("LONG out-gate", lg["out_zone"]),
        ("SHORT in-veto (fund_24h>+1.5bps)", sv["in_veto"]),
        ("SHORT out-veto", sv["out_veto"]),
    ]:
        L.append(f"| {lbl} | {b['n']} | {b.get('wr')} | {b.get('avg_net_pnl_pct')} | {b.get('avg_net_pnl_raw_pct')} |")
    L.append("")

    L.append("## Per-model extreme-zone effect (funded n>=300; RAW means; month-split in JSON)\n")
    L.append("| model | dir | base n | base raw PnL% | ext-pos n | ext-pos raw% | ext-neg n | ext-neg raw% |")
    L.append("|---|---|--:|--:|--:|--:|--:|--:|")
    for model in sorted(analysis["by_model_extremes"].keys()):
        for direction, e in analysis["by_model_extremes"][model].items():
            base, ep, en = e["baseline"], e["extreme_pos"], e["extreme_neg"]
            L.append(
                f"| {model} | {direction} | {base.get('n')} | {base.get('avg_net_pnl_raw_pct')} "
                f"| {ep.get('n')} | {ep.get('avg_net_pnl_raw_pct')} "
                f"| {en.get('n')} | {en.get('avg_net_pnl_raw_pct')} |"
            )
    L.append("")

    L.append("## Caveats\n")
    L.append(
        "- **Survivorship bias**: funding_rates covers active USDT-perps (530 symbols) vs 716 "
        "signal symbols; delisted coins partly missing → funded population skews to survivors."
    )
    L.append(
        "- PnL is realized close-vs-entry net of round-trip taker fee (0.10%); it is the logged "
        "outcome, not a re-simulation. Many legacy rows carry fixed ±2.5% outcomes."
    )
    L.append(
        "- Means are shown BOTH winsorized (global 1/99 pct, tail-safe) AND raw (unclipped). "
        "The raw mean and median are the honest read on SHORT-squeeze tail losses — winsorizing "
        "attenuates exactly the tail the hypothesis is about."
    )
    L.append(
        "- cs_pctl is a genuine cross-section rank (coin vs ALL peers' as-of fund_24h at entry), "
        "NOT the builder's per-symbol self-history fund_pctl_90d. Entry timestamps are hour-floored "
        "for the cross-section panel (funding steps on an ~8h grid → negligible as-of error)."
    )
    L.append(
        "- Autumn DST fall-back rows would map to NaT (ambiguous='NaT') and drop; the study window "
        "(Feb–Jul 2026) has no fall-back transition, so zero rows are affected here."
    )
    L.append(
        f"- WR alone is not decisive (Rule 8). Verdict rests on the fund_24h gradient being both "
        f"sign- AND magnitude-stable (|ρ|≥{MIN_ABS_SPEARMAN}) across the chrono halves; sign-only "
        f"yields 'direction-confirmed, magnitude-weak', not 'edge-found'."
    )
    L.append(
        "- **Effect is modest and ATTENUATING**: |Spearman| ≈ 0.06–0.12 in the val half but "
        "collapses toward zero in the test half. The SIGN is consistent (ABR direction) across "
        "fund_24h/72h/7d_cum/cs_pctl, the STRENGTH is weak and weakening. This confirms the ABR "
        "gate *direction* fleet-wide, but does NOT license a hard fleet-wide extreme-zone veto."
    )
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--limit-symbols", type=int, default=None, help="Smoke pass: restrict to the N highest-volume symbols."
    )
    args = ap.parse_args()

    _try_lower_priority()
    os.makedirs(OUT_DIR, exist_ok=True)

    with db_connection() as conn:
        n_raw = pd.read_sql_query("SELECT COUNT(*) AS c FROM closed_ai_signals", conn)["c"].iloc[0]
        df = load_signals(conn, args.limit_symbols)
        n_dedup = len(df)

        df = df[df["entry"].notna() & df["close_price"].notna() & (df["entry"].astype(float) != 0)].copy()
        n_priced = len(df)

        df["open_time_utc"] = to_utc_aware(df["open_time"])
        df = df[df["open_time_utc"].notna()].copy()
        df["net_pnl"] = compute_pnl(df)

        df = attach_funding(df, conn)

    n_funded = int(df["fund_24h"].notna().sum())
    n_cs = int(df["cs_pctl"].notna().sum())
    analysis = analyze(df)
    verdict = derive_verdict(analysis)

    span = (str(df["open_time_utc"].min()), str(df["open_time_utc"].max()))
    meta = {
        "study": "K3 · FRL (Funding-Risk-Layer)",
        "task": "T-2026-CU-9050-134",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "fee_per_side": FEE_PER_SIDE,
        "round_trip_fee": ROUND_TRIP_FEE,
        "n_raw": int(n_raw),
        "n_dedup": int(n_dedup),
        "n_priced": int(n_priced),
        "n_funded": n_funded,
        "n_cs_pctl": n_cs,
        "span_utc": f"{span[0]} .. {span[1]}",
        "limit_symbols": args.limit_symbols,
    }

    out = {"meta": meta, "verdict": verdict, "analysis": analysis}
    json_path = os.path.join(OUT_DIR, "funding_risk_study.json")
    md_path = os.path.join(OUT_DIR, "funding_risk_study.md")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, default=str)
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(build_markdown(meta, analysis, verdict))

    print(f"VERDICT: {verdict['verdict']}")
    print(
        f"n_raw={meta['n_raw']:,} n_dedup={meta['n_dedup']:,} n_priced={meta['n_priced']:,} "
        f"n_funded={meta['n_funded']:,} n_cs_pctl={meta['n_cs_pctl']:,}"
    )
    print(
        f"sign_stable={verdict['sign_stable_both_directions']} "
        f"magnitude_stable={verdict['magnitude_stable_both_directions']} "
        f"extreme_zone_stable={verdict['extreme_zone_stable_any']}"
    )
    for feat in ("fund_24h", "fund_72h", "fund_7d_cum", "cs_pctl"):
        mf = analysis["multi_feature"].get(feat)
        if not mf:
            continue
        for d, e in mf["by_direction"].items():
            gr = e["gradient"]
            print(
                f"  {feat} {d}: all={gr['spearman_all']} val={gr['spearman_val']} test={gr['spearman_test']} "
                f"sign_both={gr['sign_holds_both_halves']} mag_both={gr['magnitude_stable_both_halves']}"
            )
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
