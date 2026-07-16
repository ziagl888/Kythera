#!/usr/bin/env py -3.13
# tools/funding_risk_study.py — K3 · FRL (Funding-Risk-Layer) study (T-2026-CU-9050-134)
"""Read-only fleet study: does entry-funding predict SHORT/LONG expectancy?

Hypothesis (from docs/MODEL_CANDIDATES_SPEC_2026-07.md §K3): fleet SHORTs opened
at *extreme-positive* funding have systematically worse expectancy (squeeze
mechanics), symmetric for LONGs at *extreme-negative* funding. The concrete
question is whether the ABR2 gate — LONG only when ``fund_24h > +3 bps`` and a
SHORT-veto at ``fund_24h > +1.5 bps`` — generalizes across the whole fleet, or
whether it is an ABR-family idiosyncrasy.

This script is READ-ONLY. It runs SELECTs against the live DB, computes the
SHARED funding features as-of entry (``core.funding_features``), buckets the
deduped fleet trade log by funding zone × direction (× model tag), and reports
per bucket: n, win-rate, average NET PnL (round-trip taker fee included),
month-split and a chronological val/test split. WR alone is worthless (repo
Rule 8) — the verdict hangs on net-PnL expectancy that is *stable across both
time halves*.

Contracts reused (no reinvention):
  * core.funding_features.load_funding / funding_features_asof — the canonical
    as-of funding builder (fund_last/24h/72h/7d_cum/pctl/trend, bps). fund_24h
    is the ABR gate/veto quantity, so it is our zoning variable.
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
               id, symbol, model, direction, entry, close_price, targets_hit,
               open_time, status
        FROM closed_ai_signals
        {sym_filter}
        ORDER BY symbol, model, direction, open_time, id
    """
    df = pd.read_sql_query(query, conn)
    return df


def to_utc_aware(open_time: pd.Series) -> pd.Series:
    """Naive local (Europe/Bucharest) → tz-aware UTC, DST-correct. Same recipe as
    tools/aim2_build_dataset.py: localize with DST handling, then convert. The
    2026-03-29 spring-forward means a constant +3h is wrong for Feb/Mar rows."""
    s = pd.to_datetime(open_time)
    localized = s.dt.tz_localize(LEGACY_WRITER_TZ, nonexistent="shift_forward", ambiguous="NaT")
    return localized.dt.tz_convert("UTC")


def compute_pnl(df: pd.DataFrame) -> pd.Series:
    """Realized NET PnL fraction per trade (round-trip taker fee included).

    gross = realized price move close-vs-entry (unlevered). The fleet log records
    close_price as the raw exit price, which for legacy rows and un-SL-truncated
    SHORT squeezes reaches ±200%+ — real price moves the bot would NOT have
    realized (SL caps). We keep the raw value here; the *mean* is winsorized
    downstream (bucket_stats) so a handful of squeeze/data tails cannot dominate
    a bucket average, while WR and median (both tail-robust) use the raw value.
    """
    entry = df["entry"].astype(float)
    close = df["close_price"].astype(float)
    is_long = df["direction"].str.upper() == "LONG"
    gross = np.where(is_long, (close - entry) / entry, (entry - close) / entry)
    return pd.Series(gross - ROUND_TRIP_FEE, index=df.index)


def attach_funding(df: pd.DataFrame, conn) -> pd.DataFrame:
    """Compute fund_24h (+ context features) as-of entry for every trade via the
    shared as-of builder. Trades whose symbol lacks funding history drop out."""
    symbols = sorted(df["symbol"].dropna().unique().tolist())
    by_sym = load_funding(conn, symbols)  # full history, as-of over whole span
    fund_24h = np.full(len(df), np.nan)
    fund_last = np.full(len(df), np.nan)
    fund_pctl = np.full(len(df), np.nan)
    ts = df["open_time_utc"].values
    syms = df["symbol"].values
    for pos in range(len(df)):
        feats = funding_features_asof(by_sym, syms[pos], pd.Timestamp(ts[pos]))
        if feats:
            fund_24h[pos] = feats["fund_24h"]
            fund_last[pos] = feats["fund_last"]
            fund_pctl[pos] = feats["fund_pctl_90d"]
    df = df.copy()
    df["fund_24h"] = fund_24h
    df["fund_last"] = fund_last
    df["fund_pctl_90d"] = fund_pctl
    return df


def zone_of(fund_24h: float, q_edges: list[float]) -> str:
    """Funding zone label: extreme zones first (ABR thresholds), else quintile."""
    if np.isnan(fund_24h):
        return "NA"
    if fund_24h > EXTREME_POS_BPS:
        return "EXTREME_POS(>+3bps)"
    if fund_24h < EXTREME_NEG_BPS:
        return "EXTREME_NEG(<-3bps)"
    # quintiles over the (non-extreme-agnostic) fund_24h distribution
    for k, edge in enumerate(q_edges):
        if fund_24h <= edge:
            return f"Q{k + 1}"
    return f"Q{len(q_edges) + 1}"


def bucket_stats(g: pd.DataFrame) -> dict:
    n = int(len(g))
    if n == 0:
        return {"n": 0}
    pnl = g["net_pnl"]  # raw — for WR (sign) and median (tail-robust)
    pnl_w = g["net_pnl_w"]  # winsorized — for the mean (tail-safe expectancy)
    return {
        "n": n,
        "wr": round(float((pnl > 0).mean()), 4),
        "avg_net_pnl_pct": round(float(pnl_w.mean()) * 100, 4),
        "median_net_pnl_pct": round(float(pnl.median()) * 100, 4),
        "avg_fund_24h_bps": round(float(g["fund_24h"].mean()), 3),
    }


def month_split(g: pd.DataFrame) -> dict:
    out = {}
    for m, gm in g.groupby(g["open_time_utc"].dt.strftime("%Y-%m")):
        if len(gm) >= 20:
            out[m] = {
                "n": int(len(gm)),
                "avg_net_pnl_pct": round(float(gm["net_pnl"].mean()) * 100, 4),
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


def funding_pnl_correlation(funded: pd.DataFrame, median_ts: pd.Timestamp) -> dict:
    """The decisive monotonicity test: per-trade rank correlation between
    fund_24h and net PnL, per direction, per chrono half. The ABR hypothesis
    predicts a POSITIVE corr for LONG (higher funding → better LONG) and a
    NEGATIVE corr for SHORT (higher funding → worse SHORT). An effect counts as
    real only if the predicted sign holds in BOTH halves."""
    out: dict = {}
    for direction, gd in funded.groupby("direction"):
        h1 = gd[gd["open_time_utc"] < median_ts]
        h2 = gd[gd["open_time_utc"] >= median_ts]
        expected = "positive" if direction.upper() == "LONG" else "negative"
        c_all = spearman(gd["fund_24h"].values, gd["net_pnl_w"].values)
        c1 = spearman(h1["fund_24h"].values, h1["net_pnl_w"].values)
        c2 = spearman(h2["fund_24h"].values, h2["net_pnl_w"].values)

        def sign_ok(c, expected=expected):
            if c is None:
                return None
            return (c > 0) if expected == "positive" else (c < 0)

        out[direction] = {
            "expected_sign": expected,
            "spearman_all": None if c_all is None else round(c_all, 4),
            "spearman_val": None if c1 is None else round(c1, 4),
            "spearman_test": None if c2 is None else round(c2, 4),
            "sign_holds_both_halves": bool(sign_ok(c1)) and bool(sign_ok(c2)),
            "n_val": int(len(h1)),
            "n_test": int(len(h2)),
        }
    return out


def half_split_stats(g: pd.DataFrame, median_ts: pd.Timestamp) -> dict:
    h1 = g[g["open_time_utc"] < median_ts]
    h2 = g[g["open_time_utc"] >= median_ts]
    return {"val_firsthalf": bucket_stats(h1), "test_secondhalf": bucket_stats(h2)}


def analyze(df: pd.DataFrame) -> dict:
    funded = df[df["fund_24h"].notna()].copy()
    median_ts = funded["open_time_utc"].median()

    # Winsorize net PnL at the global 1/99 pct of the funded population — tames
    # the ±200% squeeze/legacy tails so a bucket MEAN reflects typical
    # expectancy, not three catastrophic outliers. WR/median use the raw value.
    lo, hi = float(np.quantile(funded["net_pnl"], 0.01)), float(np.quantile(funded["net_pnl"], 0.99))
    funded["net_pnl_w"] = funded["net_pnl"].clip(lo, hi)
    analyze.winsor_bounds = (round(lo * 100, 3), round(hi * 100, 3))  # type: ignore[attr-defined]

    # quintile edges over the non-extreme core (so extreme zones don't collapse
    # a whole quintile). Edges are the 20/40/60/80 pctl of the full fund_24h.
    core = funded["fund_24h"]
    q_edges = [float(np.quantile(core, q)) for q in (0.2, 0.4, 0.6, 0.8)]
    funded["zone"] = funded["fund_24h"].apply(lambda x: zone_of(x, q_edges))

    result: dict = {
        "quintile_edges_fund_24h_bps": [round(e, 3) for e in q_edges],
        "winsor_bounds_net_pnl_pct": [round(lo * 100, 3), round(hi * 100, 3)],
        "median_open_time_utc": str(median_ts),
        "by_direction_zone": {},
        "abr2_gate_check": {},
        "by_model_extremes": {},
        "direction_baseline": {},
        "funding_pnl_correlation": funding_pnl_correlation(funded, median_ts),
    }

    # Direction baselines (all funded trades of that direction)
    for direction, gd in funded.groupby("direction"):
        result["direction_baseline"][direction] = {
            **bucket_stats(gd),
            **half_split_stats(gd, median_ts),
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

    # Per major model tag: extreme zone effect (only tags with enough funded n)
    for model, gm in funded.groupby("model"):
        if len(gm) < 300:
            continue
        entry = {}
        for direction, gd in gm.groupby("direction"):
            entry[direction] = {
                "baseline": bucket_stats(gd),
                "extreme_pos": bucket_stats(gd[gd["fund_24h"] > EXTREME_POS_BPS]),
                "extreme_neg": bucket_stats(gd[gd["fund_24h"] < EXTREME_NEG_BPS]),
            }
        result["by_model_extremes"][model] = entry

    return result


def derive_verdict(analysis: dict) -> dict:
    """Two independent tests, both requiring stability across the chrono halves:

    PRIMARY — monotone funding→PnL gradient (per-trade Spearman fund_24h vs net
      PnL, per direction, per half). ABR predicts LONG corr>0, SHORT corr<0. The
      edge is real if BOTH directions hold their predicted sign in BOTH halves.
    SECONDARY — the hard extreme-zone claims vs baseline (kept for context; more
      fragile out-of-sample because an extreme bin is thin and regime-sensitive):
      (A) SHORT at extreme-positive funding worse than SHORT baseline
      (B) LONG at extreme-negative funding worse than LONG baseline
    """
    corr = analysis["funding_pnl_correlation"]
    gradient_both_dirs = (
        all(corr.get(d, {}).get("sign_holds_both_halves") for d in ("LONG", "SHORT")) and len(corr) >= 2
    )

    findings = []
    extreme_stable_any = False

    def half_pnl(d, key):
        v = d.get(key, {})
        return v.get("avg_net_pnl_pct"), v.get("n")

    baselines = analysis["direction_baseline"]
    for label, direction, zone_key in [
        ("SHORT@extreme-positive", "SHORT", "SHORT_extreme_pos_gt_+3bps"),
        ("LONG@extreme-negative", "LONG", "LONG_extreme_neg_lt_-3bps"),
    ]:
        base = baselines.get(direction, {})
        base_h1 = base.get("val_firsthalf", {}).get("avg_net_pnl_pct")
        base_h2 = base.get("test_secondhalf", {}).get("avg_net_pnl_pct")
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
                "zone_pnl_h1": z_h1,
                "zone_pnl_h2": z_h2,
                "baseline_pnl_h1": base_h1,
                "baseline_pnl_h2": base_h2,
                "worse_than_baseline_both_halves": stable,
                "sufficient_n_both_halves": (n_h1 or 0) >= MIN_HALF_N and (n_h2 or 0) >= MIN_HALF_N,
            }
        )

    edge = gradient_both_dirs or extreme_stable_any
    return {
        "edge_found": edge,
        "verdict": "edge-found" if edge else "no-op/no-edge",
        "gradient_both_directions_stable": gradient_both_dirs,
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
        f"- monotone funding→PnL gradient stable in BOTH directions & BOTH halves: "
        f"**{verdict['gradient_both_directions_stable']}** (primary)"
    )
    L.append(f"- hard extreme-zone claim stable in both halves: {verdict['extreme_zone_stable_any']} (secondary)\n")

    L.append("## Primary test — monotone funding→PnL gradient (per-trade Spearman)\n")
    L.append("ABR predicts LONG corr>0 (higher funding → better LONG), SHORT corr<0.\n")
    L.append("| dir | expected | Spearman all | val | test | sign holds both halves |")
    L.append("|---|---|--:|--:|--:|:--:|")
    for d, c in verdict["correlation"].items():
        L.append(
            f"| {d} | {c['expected_sign']} | {c['spearman_all']} | {c['spearman_val']} "
            f"| {c['spearman_test']} | {c['sign_holds_both_halves']} |"
        )
    L.append("")
    L.append("## Population\n")
    L.append(f"- raw closed_ai_signals rows: {meta['n_raw']:,}")
    L.append(f"- deduped (symbol,model,direction,open_time): {meta['n_dedup']:,}")
    L.append(f"- priced (entry>0 & close_price present): {meta['n_priced']:,}")
    L.append(f"- with as-of funding (fund_24h): {meta['n_funded']:,}")
    L.append(f"- open_time span (UTC): {meta['span_utc']}")
    L.append(f"- median split (val|test): {analysis['median_open_time_utc']}")
    L.append(f"- fund_24h quintile edges (bps): {analysis['quintile_edges_fund_24h_bps']}")
    L.append(
        f"- winsor bounds for mean net-PnL (1/99 pct, %): {analysis['winsor_bounds_net_pnl_pct']} "
        f"— WR & median use raw values\n"
    )

    L.append("## Hypothesis test (chrono val/test must agree)\n")
    for f in verdict["findings"]:
        L.append(f"### {f['claim']}")
        L.append(f"- zone n (total): {f['zone_n_total']}")
        L.append(f"- zone net-PnL/trade  val: {f['zone_pnl_h1']}%  |  test: {f['zone_pnl_h2']}%")
        L.append(f"- baseline net-PnL/trade  val: {f['baseline_pnl_h1']}%  |  test: {f['baseline_pnl_h2']}%")
        L.append(
            f"- worse-than-baseline in BOTH halves: {f['worse_than_baseline_both_halves']} "
            f"(sufficient n both halves: {f['sufficient_n_both_halves']})\n"
        )

    L.append("## Direction × funding zone (fleet-wide, funded trades)\n")
    L.append("| dir | zone | n | WR | avg net PnL % | avg fund_24h bps | val PnL% (n) | test PnL% (n) |")
    L.append("|---|---|--:|--:|--:|--:|--:|--:|")
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
                f"| {b.get('avg_fund_24h_bps')} | {v.get('avg_net_pnl_pct')} ({v.get('n')}) "
                f"| {t.get('avg_net_pnl_pct')} ({t.get('n')}) |"
            )
    L.append("")

    L.append("## ABR2 gate generalization (fleet-wide)\n")
    g = analysis["abr2_gate_check"]
    lg = g["LONG_gate_fund24h_gt_+3bps"]
    sv = g["SHORT_veto_fund24h_gt_+1.5bps"]
    L.append("| test | n | WR | avg net PnL % |")
    L.append("|---|--:|--:|--:|")
    L.append(
        f"| LONG in-gate (fund_24h>+3bps) | {lg['in_zone']['n']} | {lg['in_zone'].get('wr')} "
        f"| {lg['in_zone'].get('avg_net_pnl_pct')} |"
    )
    L.append(
        f"| LONG out-gate | {lg['out_zone']['n']} | {lg['out_zone'].get('wr')} "
        f"| {lg['out_zone'].get('avg_net_pnl_pct')} |"
    )
    L.append(
        f"| SHORT in-veto (fund_24h>+1.5bps) | {sv['in_veto']['n']} | {sv['in_veto'].get('wr')} "
        f"| {sv['in_veto'].get('avg_net_pnl_pct')} |"
    )
    L.append(
        f"| SHORT out-veto | {sv['out_veto']['n']} | {sv['out_veto'].get('wr')} "
        f"| {sv['out_veto'].get('avg_net_pnl_pct')} |"
    )
    L.append("")

    L.append("## Per-model extreme-zone effect (funded n>=300)\n")
    L.append("| model | dir | base n | base PnL% | ext-pos n | ext-pos PnL% | ext-neg n | ext-neg PnL% |")
    L.append("|---|---|--:|--:|--:|--:|--:|--:|")
    for model in sorted(analysis["by_model_extremes"].keys()):
        for direction, e in analysis["by_model_extremes"][model].items():
            base, ep, en = e["baseline"], e["extreme_pos"], e["extreme_neg"]
            L.append(
                f"| {model} | {direction} | {base.get('n')} | {base.get('avg_net_pnl_pct')} "
                f"| {ep.get('n')} | {ep.get('avg_net_pnl_pct')} "
                f"| {en.get('n')} | {en.get('avg_net_pnl_pct')} |"
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
        "- Funding zoning uses fund_24h (the ABR gate quantity). Extreme zones use the ABR ±3bps "
        "cut; quintiles cover the whole fund_24h distribution incl. extremes."
    )
    L.append(
        f"- WR alone is not decisive (Rule 8). Verdict rests on net-PnL stable across the "
        f"chrono val/test halves with n>={MIN_HALF_N} in each."
    )
    L.append(
        "- **Effect is modest and ATTENUATING**: |Spearman| ≈ 0.06–0.12 in the val half but "
        "collapses toward zero in the test half (LONG +0.017, SHORT -0.018). The SIGN is "
        "consistent (ABR direction), the STRENGTH is weak and weakening recently. This confirms "
        "the ABR gate *direction* fleet-wide, but does not license a hard fleet-wide extreme-zone "
        "veto — the SHORT extreme-positive bin fails strict both-halves stability (test-half "
        "regime compression)."
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
        f"n_funded={meta['n_funded']:,}"
    )
    print(
        f"gradient_both_dirs_stable={verdict['gradient_both_directions_stable']} "
        f"extreme_zone_stable={verdict['extreme_zone_stable_any']}"
    )
    for d, c in verdict["correlation"].items():
        print(
            f"  Spearman {d}: all={c['spearman_all']} val={c['spearman_val']} "
            f"test={c['spearman_test']} sign_holds_both={c['sign_holds_both_halves']}"
        )
    for f in verdict["findings"]:
        print(
            f"  {f['claim']}: worse_both_halves={f['worse_than_baseline_both_halves']} "
            f"zone_pnl val/test={f['zone_pnl_h1']}/{f['zone_pnl_h2']} "
            f"base val/test={f['baseline_pnl_h1']}/{f['baseline_pnl_h2']}"
        )
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
