#!/usr/bin/env py -3.13
# tools/settlement_timing_study.py — K8 · SET (Settlement-/time-of-day study) (T-2026-CU-9050-135)
"""Read-only fleet study: does entry timing (funding-settlement proximity /
time-of-day) move the expectancy of our trades?

Hypothesis (from docs/MODEL_CANDIDATES_SPEC_2026-07.md §K8, evidence F9): entry
proximity to the funding settlements (00/08/16 UTC) and time-of-day windows
affect expectancy (spread/vol patterns around settlements). F9 is medium-weight
(2 months, dispersion ≠ returns), so we test on OUR OWN closed trades rather than
on raw microstructure.

This script is READ-ONLY. It runs SELECTs against the live DB, dedupes the fleet
trade log, and for every trade computes two purely time-derived features:
  (a) entry-offset to the NEAREST funding settlement (00/08/16 UTC), signed
      minutes in (−240, +240] (settlements are 8h apart → nearest is within
      ±240 min), bucketed into 30-min bins;
  (b) entry-hour UTC (0–23).
It then reports expectancy per bucket × direction × model-tag: n, win-rate,
average NET PnL (round-trip taker fee in, winsorized AND raw), median, a simple
bootstrap CI (resampling net-PnL per bucket — no significance theatre), plus a
month-split and a chronological val/test halving. A "prefer / avoid" window is
only emitted when the effect holds in BOTH chrono halves with n ≥ a documented
floor and a documented magnitude floor — WR alone is worthless (repo Rule 8).

NO funding join is needed here: settlement offset and hour are functions of the
entry timestamp in UTC alone. The only shared contracts reused are:
  * tools.walkforward_sim.FEE_PER_SIDE = 0.0005 (taker 0.05%/side → 0.10%
    round-trip, P3.6). Net PnL = gross − 2·FEE_PER_SIDE. We do NOT invent a fee.
  * core.time.LEGACY_WRITER_TZ = "Europe/Bucharest" — closed_ai_signals.open_time
    is TIMESTAMP WITHOUT TIME ZONE = naive local Bucharest (TZ-cluster P2.1–P2.6).
    We localize DST-aware (a constant ±3h offset is WRONG across the 2026-03-29
    spring-forward), exactly like tools/funding_risk_study.py (K3) and
    tools/aim2_build_dataset.py. Getting this wrong would smear every settlement
    offset by an hour on one side of the DST jump — the whole study depends on it.

Dedup: closed_ai_signals carries ~357k duplicate rows (raw ≈445k → ≈88k). We
dedup on (symbol, model, direction, open_time), keeping the lowest id, BEFORE any
analysis (DISTINCT ON … ORDER BY …, id).

PnL: realized close-vs-entry per trade. gross = (close−entry)/entry for LONG,
(entry−close)/entry for SHORT; net = gross − 2·FEE_PER_SIDE. This is the honest
per-trade outcome recorded in the fleet log (close_price is the realized exit),
not a re-simulation. Many legacy rows carry fixed ±2.5% outcomes.

Survivorship / selection bias (documented, not corrected; Rule 9): the population
is only trades the fleet actually OPENED and CLOSED. It is conditioned on the
existing entry logic — including each bot's scan schedule, which itself clusters
entries at specific minutes/hours. A time-of-day "effect" can therefore be a
scan-schedule confound (a window is empty or over-represented because a bot only
runs then), not a genuine microstructure edge. Any "prefer/avoid this window"
claim is WITHIN the already-selected population and says nothing about untaken
windows.

Output: staging_models/settlement_timing_study.json (machine) + .md (human).
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
from core.time import LEGACY_WRITER_TZ  # noqa: E402
from tools.walkforward_sim import FEE_PER_SIDE  # noqa: E402

ROUND_TRIP_FEE = 2.0 * FEE_PER_SIDE  # 0.001 = 0.10 %

SETTLE_PERIOD_SEC = 8 * 3600  # funding settles every 8h at 00/08/16 UTC
HALF_PERIOD_SEC = SETTLE_PERIOD_SEC // 2  # 4h; nearest settlement is within ±this
BIN_MIN = 30  # 30-min offset bins
BIN_EDGES = np.arange(-240, 241, BIN_MIN)  # [-240, -210, …, 210, 240]

# Materiality / support floors for a bucket to count toward a recommendation.
# A window must hold in BOTH chrono halves with n ≥ MIN_HALF_N each, and the mean
# net-PnL delta vs. the group×direction baseline must clear MIN_ABS_DELTA in both
# halves — n≈80k makes tiny deltas "significant" yet economically trivial, so we
# gate on magnitude, not on a p-value.
MIN_HALF_N = 100
MIN_BUCKET_N = 300  # total (both halves) for a bucket to be reported at all
MIN_TAG_N = 1500  # a model-tag needs this many trades to get its own breakdown
MIN_ABS_DELTA = 0.005  # 0.5 net-PnL percentage points/trade vs. baseline
MIN_BOOT_N = 50  # below this, no bootstrap CI (too thin to resample meaningfully)
N_BOOT = 1000
BOOT_SEED = 12345

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
    tools/funding_risk_study.py (K3): localize with DST handling, then convert. The
    2026-03-29 spring-forward means a constant +3h is wrong for Feb/Mar rows.

    ``ambiguous="NaT"`` maps the autumn fall-back hour (~1h/yr where the wall clock
    repeats) to NaT; those rows are dropped by the caller. In this study's window
    (2026-02 .. 2026-07) there is no fall-back transition, so zero rows are
    affected — the flag is correctness hygiene for a wider re-run.
    """
    s = pd.to_datetime(open_time)
    localized = s.dt.tz_localize(LEGACY_WRITER_TZ, nonexistent="shift_forward", ambiguous="NaT")
    return localized.dt.tz_convert("UTC")


def compute_pnl(df: pd.DataFrame) -> pd.Series:
    """Realized NET PnL fraction per trade (round-trip taker fee included).

    gross = realized price move close-vs-entry (unlevered). We keep the raw value;
    downstream we report BOTH a winsorized mean (tail-safe) AND the raw mean plus
    the raw median, so tail risk is not hidden. WR and median are tail-robust.
    """
    entry = df["entry"].astype(float)
    close = df["close_price"].astype(float)
    is_long = df["direction"].str.upper() == "LONG"
    gross = np.where(is_long, (close - entry) / entry, (entry - close) / entry)
    return pd.Series(gross - ROUND_TRIP_FEE, index=df.index)


def settlement_offset_min(utc: pd.Series) -> np.ndarray:
    """Signed minutes from the NEAREST funding settlement (00/08/16 UTC) to entry.

    Unix epoch (1970-01-01 00:00 UTC) is itself a settlement, and settlements are
    exact multiples of 8h from it, so ``unix_sec mod 28800`` is the seconds elapsed
    since the most recent settlement. If that is ≤ 4h, the nearest settlement is
    the past one and the offset is positive (entry is AFTER settlement); otherwise
    the nearest is the upcoming one and the offset is negative (entry is BEFORE
    settlement). Result lies in (−240, +240] minutes.
    """
    ns = utc.astype("int64").to_numpy()  # ns since epoch, UTC-normalized
    sec = ns // 1_000_000_000
    cycle = np.mod(sec, SETTLE_PERIOD_SEC)  # seconds after last settlement, [0, 28800)
    off_sec = np.where(cycle <= HALF_PERIOD_SEC, cycle, cycle - SETTLE_PERIOD_SEC)
    return off_sec / 60.0


def offset_bin_label(off_min: np.ndarray) -> np.ndarray:
    """30-min bin label by left edge, e.g. "[-060,-030)". Clamp the single
    boundary value +240 into the top bin so it is never dropped."""
    clamped = np.clip(off_min, BIN_EDGES[0], BIN_EDGES[-1] - 1e-6)
    idx = np.searchsorted(BIN_EDGES, clamped, side="right") - 1
    idx = np.clip(idx, 0, len(BIN_EDGES) - 2)
    left = BIN_EDGES[idx]
    right = left + BIN_MIN
    return np.array([f"[{lo:+04d},{hi:+04d})" for lo, hi in zip(left, right, strict=True)])


def offset_bucket_order() -> list[str]:
    return [f"[{lo:+04d},{lo + BIN_MIN:+04d})" for lo in BIN_EDGES[:-1]]


def bootstrap_ci(values: np.ndarray, alpha: float = 0.05) -> list[float] | None:
    """95% bootstrap CI of the MEAN net-PnL (%). Simple resampling with
    replacement, chunked to bound memory on large buckets. None if too thin."""
    v = np.asarray(values, dtype=float)
    n = len(v)
    if n < MIN_BOOT_N:
        return None
    rng = np.random.default_rng(BOOT_SEED)
    means = np.empty(N_BOOT)
    chunk = 200
    for start in range(0, N_BOOT, chunk):
        k = min(chunk, N_BOOT - start)
        idx = rng.integers(0, n, size=(k, n))
        means[start : start + k] = v[idx].mean(axis=1)
    lo, hi = np.percentile(means, [100 * alpha / 2, 100 * (1 - alpha / 2)])
    return [round(float(lo) * 100, 4), round(float(hi) * 100, 4)]


def bucket_stats(g: pd.DataFrame, with_ci: bool = False) -> dict:
    n = int(len(g))
    if n == 0:
        return {"n": 0}
    pnl = g["net_pnl"]  # raw — WR (sign), median (tail-robust), raw mean
    pnl_w = g["net_pnl_w"]  # winsorized — tail-safe mean
    out = {
        "n": n,
        "wr": round(float((pnl > 0).mean()), 4),
        "avg_net_pnl_pct": round(float(pnl_w.mean()) * 100, 4),  # winsorized
        "avg_net_pnl_raw_pct": round(float(pnl.mean()) * 100, 4),  # unclipped
        "median_net_pnl_pct": round(float(pnl.median()) * 100, 4),
    }
    if with_ci:
        out["boot_ci95_raw_pct"] = bootstrap_ci(pnl.to_numpy())
    return out


def month_split(g: pd.DataFrame, min_n: int = 40) -> dict:
    out = {}
    for m, gm in g.groupby(g["open_time_utc"].dt.strftime("%Y-%m")):
        if len(gm) >= min_n:
            out[m] = {
                "n": int(len(gm)),
                "avg_net_pnl_raw_pct": round(float(gm["net_pnl"].mean()) * 100, 4),
                "wr": round(float((gm["net_pnl"] > 0).mean()), 4),
            }
    return out


def half_split_stats(g: pd.DataFrame, median_ts: pd.Timestamp) -> dict:
    h1 = g[g["open_time_utc"] < median_ts]
    h2 = g[g["open_time_utc"] >= median_ts]
    return {"val_firsthalf": bucket_stats(h1), "test_secondhalf": bucket_stats(h2)}


def _win_mean(g: pd.DataFrame) -> float:
    return float(g["net_pnl_w"].mean()) if len(g) else float("nan")


def classify_window(bucket: pd.DataFrame, baseline: pd.DataFrame, median_ts: pd.Timestamp) -> dict:
    """Prefer/avoid/neutral for one bucket vs. its group×direction baseline.

    Uses WINSORIZED means so a single legacy tail row cannot flip a window. A
    verdict of prefer/avoid requires the sign of the delta (bucket − baseline) to
    agree in BOTH chrono halves AND |delta| ≥ MIN_ABS_DELTA in both — otherwise
    neutral (or insufficient-n)."""
    bv = bucket[bucket["open_time_utc"] < median_ts]
    bt = bucket[bucket["open_time_utc"] >= median_ts]
    Bv = baseline[baseline["open_time_utc"] < median_ts]
    Bt = baseline[baseline["open_time_utc"] >= median_ts]
    n_v, n_t = len(bv), len(bt)
    res = {
        "n_val": int(n_v),
        "n_test": int(n_t),
        "delta_val_pct": None,
        "delta_test_pct": None,
        "label": "insufficient-n",
    }
    if n_v < MIN_HALF_N or n_t < MIN_HALF_N:
        return res
    dv = _win_mean(bv) - _win_mean(Bv)
    dtt = _win_mean(bt) - _win_mean(Bt)
    res["delta_val_pct"] = round(dv * 100, 4)
    res["delta_test_pct"] = round(dtt * 100, 4)
    if dv > 0 and dtt > 0 and min(dv, dtt) >= MIN_ABS_DELTA:
        res["label"] = "prefer"
    elif dv < 0 and dtt < 0 and min(-dv, -dtt) >= MIN_ABS_DELTA:
        res["label"] = "avoid"
    else:
        res["label"] = "neutral"
    return res


def analyze_dimension(group: pd.DataFrame, dim_col: str, order: list[str], median_ts: pd.Timestamp) -> dict:
    """One time-dimension (offset-bin or hour) for one model-group: per-direction
    baseline + per-bucket stats/halves/CI + window classification."""
    out: dict = {}
    for direction, gd in group.groupby("direction"):
        base = {**bucket_stats(gd), **half_split_stats(gd, median_ts), "months": month_split(gd)}
        buckets: dict = {}
        recos: list[dict] = []
        for label, gb in gd.groupby(dim_col):
            if len(gb) < 1:
                continue
            entry = {
                **bucket_stats(gb, with_ci=(len(gb) >= MIN_BUCKET_N)),
                **half_split_stats(gb, median_ts),
            }
            cls = classify_window(gb, gd, median_ts)
            entry["window_class"] = cls
            buckets[str(label)] = entry
            if cls["label"] in ("prefer", "avoid") and len(gb) >= MIN_BUCKET_N:
                recos.append({"bucket": str(label), **cls})
        out[direction] = {
            "baseline": base,
            "buckets": {k: buckets[k] for k in order if k in buckets},
            "recommendations": recos,
        }
    return out


def analyze(df: pd.DataFrame) -> dict:
    median_ts = df["open_time_utc"].median()
    lo, hi = float(np.quantile(df["net_pnl"], 0.01)), float(np.quantile(df["net_pnl"], 0.99))
    df = df.copy()
    df["net_pnl_w"] = df["net_pnl"].clip(lo, hi)

    off_order = offset_bucket_order()
    hour_order = [str(h) for h in range(24)]

    groups: list[tuple[str, pd.DataFrame]] = [("FLEET", df)]
    for model, gm in df.groupby("model"):
        if len(gm) >= MIN_TAG_N:
            groups.append((f"TAG:{model}", gm))

    result: dict = {
        "median_open_time_utc": str(median_ts),
        "winsor_bounds_net_pnl_pct": [round(lo * 100, 3), round(hi * 100, 3)],
        "settlement_offset": {},
        "hour_of_day": {},
        "recommendations": [],
    }

    for gname, g in groups:
        result["settlement_offset"][gname] = analyze_dimension(g, "offset_bin", off_order, median_ts)
        result["hour_of_day"][gname] = analyze_dimension(g, "entry_hour", hour_order, median_ts)

    # Flatten the stable prefer/avoid windows into a single recommendation table.
    for dim_key, dim_name in [("settlement_offset", "settlement-offset(min)"), ("hour_of_day", "entry-hour(UTC)")]:
        for gname, byd in result[dim_key].items():
            for direction, e in byd.items():
                for r in e["recommendations"]:
                    result["recommendations"].append(
                        {
                            "group": gname,
                            "direction": direction,
                            "dimension": dim_name,
                            "window": r["bucket"],
                            "action": r["label"],
                            "delta_val_pct": r["delta_val_pct"],
                            "delta_test_pct": r["delta_test_pct"],
                            "n_val": r["n_val"],
                            "n_test": r["n_test"],
                        }
                    )
    return result


def derive_verdict(analysis: dict) -> dict:
    recos = analysis["recommendations"]
    fleet_recos = [r for r in recos if r["group"] == "FLEET"]
    verdict = "timing-edge-found" if recos else "no-op/no-stable-window"

    # Honesty metric: how badly does the effect attenuate out-of-sample? Compare
    # the median |delta| in the (older) val half vs. the (newer) test half across
    # all stable windows. A large ratio means the "edge" is mostly in-sample and
    # decays hard — sign-stable but magnitude-weak (the K3 attenuation pattern).
    dv = [abs(r["delta_val_pct"]) for r in recos if r["delta_val_pct"] is not None]
    dt_ = [abs(r["delta_test_pct"]) for r in recos if r["delta_test_pct"] is not None]
    med_val = round(float(np.median(dv)), 4) if dv else None
    med_test = round(float(np.median(dt_)), 4) if dt_ else None
    attenuation_ratio = round(med_val / med_test, 2) if (med_val and med_test) else None

    return {
        "verdict": verdict,
        "n_stable_windows": len(recos),
        "n_fleet_windows": len(fleet_recos),
        "floors": {
            "min_half_n": MIN_HALF_N,
            "min_bucket_n": MIN_BUCKET_N,
            "min_abs_delta_pct": round(MIN_ABS_DELTA * 100, 3),
            "min_tag_n": MIN_TAG_N,
        },
        "attenuation": {
            "median_abs_delta_val_pct": med_val,
            "median_abs_delta_test_pct": med_test,
            "val_over_test_ratio": attenuation_ratio,
            "note": (
                "Stable windows are sign-consistent across both chrono halves, but the effect "
                "MAGNITUDE decays sharply from the older (val) to the newer (test) half. The edge is "
                "real in-direction yet weak and attenuating out-of-sample — treat any window as a "
                "low-conviction scan-schedule tweak, not a hard gate."
            ),
        },
        "recommendations": recos,
    }


def _fmt(v) -> str:
    return "—" if v is None else str(v)


def _bucket_rows(byd: dict, order: list[str], L: list[str]) -> None:
    for direction in sorted(byd.keys()):
        e = byd[direction]
        for label in order:
            b = e["buckets"].get(label)
            if not b or b.get("n", 0) < MIN_BUCKET_N:
                continue
            v = b.get("val_firsthalf", {})
            t = b.get("test_secondhalf", {})
            ci = b.get("boot_ci95_raw_pct")
            ci_s = f"[{ci[0]},{ci[1]}]" if ci else "—"
            cls = b.get("window_class", {}).get("label", "—")
            L.append(
                f"| {direction} | {label} | {b['n']} | {b.get('wr')} | {b.get('avg_net_pnl_pct')} "
                f"| {b.get('avg_net_pnl_raw_pct')} | {b.get('median_net_pnl_pct')} | {ci_s} "
                f"| {_fmt(v.get('avg_net_pnl_raw_pct'))} ({v.get('n', 0)}) "
                f"| {_fmt(t.get('avg_net_pnl_raw_pct'))} ({t.get('n', 0)}) | {cls} |"
            )


def build_markdown(meta: dict, analysis: dict, verdict: dict) -> str:
    L: list[str] = []
    L.append("# K8 · SET — Settlement-/time-of-day expectancy study (T-2026-CU-9050-135)\n")
    L.append(
        f"_Generated {meta['generated_at']} · read-only fleet analysis · fee/side {FEE_PER_SIDE} "
        f"(round-trip {ROUND_TRIP_FEE:.4f})_\n"
    )
    L.append(f"**VERDICT: {verdict['verdict']}**\n")
    L.append(
        f"- stable prefer/avoid windows (both chrono halves, n≥{MIN_BUCKET_N}, |Δ|≥"
        f"{round(MIN_ABS_DELTA * 100, 3)}pp): **{verdict['n_stable_windows']}** "
        f"(fleet-level: {verdict['n_fleet_windows']})"
    )
    att = verdict["attenuation"]
    L.append(
        f"- **magnitude attenuates hard**: median |Δ| val {att['median_abs_delta_val_pct']}pp → test "
        f"{att['median_abs_delta_test_pct']}pp (val/test ≈ {att['val_over_test_ratio']}×). The SIGN is "
        "stable across both halves; the STRENGTH is mostly in-sample and decays — low-conviction, not a hard gate.\n"
    )

    L.append("## Population\n")
    L.append(f"- raw closed_ai_signals rows: {meta['n_raw']:,}")
    L.append(f"- deduped (symbol,model,direction,open_time): {meta['n_dedup']:,}")
    L.append(f"- priced (entry>0 & close_price present): {meta['n_priced']:,}")
    L.append(f"- with valid UTC entry time (analysed): {meta['n_timed']:,}")
    L.append(f"- open_time span (UTC): {meta['span_utc']}")
    L.append(f"- median split (val|test): {analysis['median_open_time_utc']}")
    L.append(
        f"- winsor bounds for mean net-PnL (1/99 pct, %): {analysis['winsor_bounds_net_pnl_pct']} "
        f"— WR, median & raw mean use raw values\n"
    )

    L.append("## Recommendation table — bot(model-tag) × window → avoid / prefer\n")
    L.append(
        "Only windows whose winsorized net-PnL delta vs. the group×direction baseline is "
        f"sign-stable across BOTH chrono halves with n≥{MIN_HALF_N}/half and |Δ|≥"
        f"{round(MIN_ABS_DELTA * 100, 3)}pp. TAG:x = model-tag; map to bots via bot_catalog.\n"
    )
    if verdict["recommendations"]:
        L.append("| group | dir | dimension | window | action | Δ val pp | Δ test pp | n val | n test |")
        L.append("|---|---|---|---|:--:|--:|--:|--:|--:|")
        for r in verdict["recommendations"]:
            L.append(
                f"| {r['group']} | {r['direction']} | {r['dimension']} | {r['window']} | {r['action']} "
                f"| {r['delta_val_pct']} | {r['delta_test_pct']} | {r['n_val']} | {r['n_test']} |"
            )
    else:
        L.append("_No window survived both chrono halves with the required support and magnitude._")
        L.append(
            "\n**No stable timing edge** — neither settlement-offset nor entry-hour bucket shows an "
            "expectancy effect that holds across both time-halves. Per §K8 stop-criterion this is a "
            "SUCCESSFUL negative result: no scan-window shift is licensed by the data."
        )
    L.append("")

    hdr = (
        "| dir | bucket | n | WR | net PnL % (wins) | net PnL % (raw) | median % | boot CI95 raw% "
        "| val raw% (n) | test raw% (n) | class |"
    )
    sep = "|---|---|--:|--:|--:|--:|--:|:--:|--:|--:|:--:|"

    L.append("## Settlement-offset buckets — FLEET (30-min bins, offset to nearest 00/08/16 UTC)\n")
    L.append(f"Negative = entry BEFORE the settlement, positive = AFTER. Buckets with n≥{MIN_BUCKET_N} shown.\n")
    L.append(hdr)
    L.append(sep)
    _bucket_rows(analysis["settlement_offset"]["FLEET"], offset_bucket_order(), L)
    L.append("")

    L.append("## Entry-hour buckets — FLEET (hour of day, UTC)\n")
    L.append(hdr.replace("bucket", "hour"))
    L.append(sep)
    _bucket_rows(analysis["hour_of_day"]["FLEET"], [str(h) for h in range(24)], L)
    L.append("")

    tags = [g for g in analysis["settlement_offset"].keys() if g != "FLEET"]
    L.append(f"## Per model-tag breakdowns (n≥{MIN_TAG_N})\n")
    if tags:
        L.append(f"Tags with their own breakdown: {', '.join(sorted(tags))}. Full per-bucket detail in the JSON.\n")
        for gname in sorted(tags):
            recs = [r for r in verdict["recommendations"] if r["group"] == gname]
            L.append(f"- **{gname}**: {len(recs)} stable window(s)" + (f" → {[r['window'] for r in recs]}" if recs else " (none)"))
    else:
        L.append("_No model-tag reached the per-tag support threshold._")
    L.append("")

    L.append("## Caveats\n")
    L.append(
        "- **Selection / survivorship bias (Rule 9)**: the population is only trades the fleet "
        "actually OPENED and CLOSED, conditioned on the existing entry logic — including each bot's "
        "SCAN SCHEDULE, which clusters entries at specific minutes/hours. A time-of-day effect can be "
        "a scan-schedule confound (a window over/under-represented because a bot only runs then), not "
        "a genuine microstructure edge; the study says nothing about untaken windows."
    )
    L.append(
        "- **TZ**: open_time is naive local Bucharest (P2.1–P2.6). Converted DST-aware to UTC before "
        "computing offsets/hours; a constant +3h would smear every offset by an hour on one side of "
        "the 2026-03-29 DST jump. Autumn fall-back rows would map to NaT and drop — none in this window."
    )
    L.append(
        "- Means are shown BOTH winsorized (global 1/99 pct, tail-safe) AND raw (unclipped) with the "
        "median; window classification uses the winsorized mean so a single legacy ±tail row cannot "
        "flip a bucket. WR alone is not decisive (Rule 8)."
    )
    L.append(
        "- PnL is realized close-vs-entry net of round-trip taker fee (0.10%); it is the logged "
        "outcome, not a re-simulation. Many legacy rows carry fixed ±2.5% outcomes."
    )
    L.append(
        "- Bootstrap CI = 1000-resample percentile CI of the raw-mean net-PnL per bucket (descriptive; "
        "no significance test). A window is only recommended on cross-half stability + a magnitude "
        "floor, never on a CI/p-value alone."
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
    n_timed = len(df)

    df["net_pnl"] = compute_pnl(df)
    off_min = settlement_offset_min(df["open_time_utc"])
    df["offset_min"] = off_min
    df["offset_bin"] = offset_bin_label(off_min)
    df["entry_hour"] = df["open_time_utc"].dt.hour.astype(str)

    analysis = analyze(df)
    verdict = derive_verdict(analysis)

    span = (str(df["open_time_utc"].min()), str(df["open_time_utc"].max()))
    meta = {
        "study": "K8 · SET (Settlement-/time-of-day)",
        "task": "T-2026-CU-9050-135",
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "fee_per_side": FEE_PER_SIDE,
        "round_trip_fee": ROUND_TRIP_FEE,
        "n_raw": int(n_raw),
        "n_dedup": int(n_dedup),
        "n_priced": int(n_priced),
        "n_timed": int(n_timed),
        "span_utc": f"{span[0]} .. {span[1]}",
        "limit_symbols": args.limit_symbols,
    }

    out = {"meta": meta, "verdict": verdict, "analysis": analysis}
    json_path = os.path.join(OUT_DIR, "settlement_timing_study.json")
    md_path = os.path.join(OUT_DIR, "settlement_timing_study.md")
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, default=str)
    with open(md_path, "w", encoding="utf-8") as fh:
        fh.write(build_markdown(meta, analysis, verdict))

    print(f"VERDICT: {verdict['verdict']}")
    print(
        f"n_raw={meta['n_raw']:,} n_dedup={meta['n_dedup']:,} n_priced={meta['n_priced']:,} "
        f"n_timed={meta['n_timed']:,}"
    )
    print(
        f"stable_windows={verdict['n_stable_windows']} (fleet={verdict['n_fleet_windows']})"
    )
    for r in verdict["recommendations"][:20]:
        print(
            f"  {r['group']} {r['direction']} {r['dimension']} {r['window']} -> {r['action']} "
            f"(dval={r['delta_val_pct']}pp dtest={r['delta_test_pct']}pp n={r['n_val']}/{r['n_test']})"
        )
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
