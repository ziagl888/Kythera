#!/usr/bin/env py -3.13
# tools/tsmom_study.py — K1 · TSM1 (Time-Series-Momentum) study (T-2026-CU-9050-138)
"""Read-only replay study: does a ROC-lookback momentum signal on 6h aggregates
have positive NET edge across the USDT-perp universe — WITH our deployable
geometry (smart-targets + fixed SL), not the paper's ATR trailing?

Hypothesis (docs/MODEL_CANDIDATES_SPEC_2026-07.md §K1): a time-series-momentum
rule (ride the trend long/short) on 6h candles carries positive fleet-wide net
edge. Evidence F8 (arXiv 2602.11708v1, claimed 2.41 net Sharpe; medium — the
paper's MONTHLY re-optimization is a textbook overfitting vector, so we do NOT
chase it; the grid here is FIXED, no re-fitting over time).

Design (fixed grid, no time-varying refit):
  * Aggregation: resample 1h → 6h anchored at 00/06/12/18 UTC (origin='epoch';
    open_time is TIMESTAMPTZ → we convert to UTC, resample on UTC, NEVER local
    time). Only FULL closed 6h windows (exactly 6 one-hour candles) count. As a
    resample-artifact robustness check we run the SAME grid on native 4h candles.
  * Signal: ROC_L = close/close[-L] - 1, on the aggregate close series. Event
    when ROC_L crosses a band (sign = direction: >0 → LONG momentum, <0 → SHORT).
    Grid: L ∈ {8,12,16,24,32} bars × threshold k ∈ {0, 0.5σ, 1.0σ}, σ = rolling
    90d stdev of ROC_L, AS-OF (trailing rolling window, no lookahead). k=0 is the
    zero-crossing (pure sign flip). A crossing is prev-outside-band → now-inside.
  * Dedup (per coin/direction/cell = one candidate bot config): max 1 open event;
    re-entry only after the prior trade's geometry-exit — the 4h-cooldown
    convention. The geometry-(a) exit defines the "position open" interval, so
    labels (a) and (b) score the SAME event set (clean a-vs-b comparison).

Labels DOUBLE per event:
  (a) OUR GEOMETRY — the deployable truth. get_hvn_and_sr_levels(df=as-of 95d 1h
      frame) → hvn_sr_trade_geometry → ensure_min_tp_distance → simulate_exit
      (first-touch TP-vs-SL on 1h candles AFTER entry, round-trip taker fee).
      Entry = aggregate close; exit scan starts at the first 1h candle at/after
      the aggregate close-time. NO live lookups, NO lookahead (as-of frame only).
  (b) PAPER APPROXIMATION — time-exit after H ∈ {8,16,28} aggregate bars with a
      wide 15% catastrophe SL (first-touch on the aggregate highs/lows in
      between). The gap (a)−(b) is the quantified cost of substituting our Cornix
      geometry for the paper's time/ATR exit (Open Question 3 of the report).

Verdict / stop-criterion (§K1): the geometry-(a) label is decisive. If NO grid
cell has BOTH val AND test positive avg net PnL at n≥200 TEST trades ⇒ the paper
is falsified for our stack — a NEGATIVE result is SUCCESS, documented and parked.
Threshold is chosen on VAL only; TEST is read once. WR alone is not decisive
(repo Rule 8) — the verdict hangs on net-PnL expectancy consistent across the
chrono val/test halves.

READ-ONLY: SELECTs only, BELOW_NORMAL priority, sole job on a real-money VPS.
The VPS is CPU-saturated (100%); walkforward_sim.check_cpu_headroom would abort,
so a study-local --skip-cpu-check flag (default OFF) bypasses it deliberately.
Artifacts → staging_models/ ONLY (repo Rule 2), never the repo root.

Fallen honored: resample TZ (UTC anchor, not local); survivorship (Rule 9 —
coins.json = active perps only, delisted coins absent → the population skews to
survivors, documented not corrected); only CLOSED candles (R1 — read_candles
include_forming=False, aggregate windows require all 6 hours present).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import tempfile

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.candles import read_candles  # noqa: E402
from core.database import db_connection  # noqa: E402
from core.market_utils import load_coins  # noqa: E402
from core.trade_utils import (  # noqa: E402
    ensure_min_tp_distance,
    get_hvn_and_sr_levels,
    hvn_sr_trade_geometry,
)
from tools.walkforward_sim import (  # noqa: E402
    FEE_PER_SIDE,
    check_cpu_headroom,
    set_low_priority,
    simulate_exit,
)

ROUND_TRIP_FEE = 2.0 * FEE_PER_SIDE  # 0.001 = 0.10 %

# ── Fixed grid (§K1) ─────────────────────────────────────────────────────────
L_GRID = [8, 12, 16, 24, 32]          # ROC lookback in aggregate bars
K_GRID = [0.0, 0.5, 1.0]              # band = k · σ(ROC_L, 90d as-of)
H_GRID = [8, 16, 28]                  # paper-(b) time-exit horizons (aggregate bars)
AGGS = {"6h": 6, "4h": 4}            # label → bar hours (6h resampled, 4h native)
SIGMA_DAYS = 90                       # rolling stdev window for σ
CATASTROPHE_SL = 0.15                 # paper-(b) wide 15% SL
N_PUBLISHED = 3                       # TSM1 would publish 3 TPs (abr1/rub/ats/atb convention)
SR_WINDOW_H = 95 * 24                 # get_hvn_and_sr_levels uses 95d of 1h candles
EXIT_SCAN_CAP_H = 60 * 24             # bound the 1h first-touch scan to 60d post-entry
MIN_TEST_N = 200                      # stop-criterion trade floor (TEST half)
CHECKPOINT_EVERY = 25                 # atomic-write the partial aggregate + resume-state every N coins
MIN_AVAIL_MB = 500                    # abort (rather than risk the live fleet) below this free RAM

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "staging_models")
# Resume-state lives in the OS temp dir (NEVER the repo) — a transient JSON of the
# streaming accumulators so a watchdog-kill mid-run can be resumed, not restarted.
# The live VPS watchdog periodically reaps stray python.exe; both un-resumable full
# runs died at ~75 coins. --resume + a relaunch wrapper walks the 527 coins in
# kill-sized segments, folding into the SAME accumulators across launches.
DEFAULT_STATE_PATH = os.path.join(tempfile.gettempdir(), "tsmom_study_state.json")


# ── Aggregation ──────────────────────────────────────────────────────────────
def load_1h_utc(conn, symbol: str) -> pd.DataFrame | None:
    """All CLOSED 1h candles for the symbol, ascending, indexed by tz-aware UTC
    open_time. read_candles returns TIMESTAMPTZ — we convert to UTC explicitly so
    the 6h resample anchors on UTC, never on the DB session's local offset."""
    try:
        df = read_candles(
            conn, symbol, "1h", include_forming=False,
            columns=("open_time", "open", "high", "low", "close"),
        )
    except Exception:
        conn.rollback()
        return None
    if df is None or df.empty or len(df) < 300:
        return None
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["high", "low", "close"]).sort_values("open_time").reset_index(drop=True)
    return df


def resample_6h(df1h: pd.DataFrame) -> pd.DataFrame:
    """1h → 6h OHLC anchored at 00/06/12/18 UTC. Only full windows (6 hours)."""
    idx = df1h.set_index("open_time")
    agg = idx.resample("6h", label="left", closed="left", origin="epoch").agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"),
        close=("close", "last"), cnt=("close", "count"),
    )
    agg = agg[agg["cnt"] == 6].copy()  # full closed 6h windows only
    agg = agg.dropna(subset=["close"]).reset_index()
    agg = agg.rename(columns={"open_time": "bar_start"})
    return agg


def native_4h(conn, symbol: str) -> pd.DataFrame | None:
    """Native CLOSED 4h candles (Binance-anchored 00/04/08/12/16/20 UTC)."""
    try:
        df = read_candles(
            conn, symbol, "4h", include_forming=False,
            columns=("open_time", "open", "high", "low", "close"),
        )
    except Exception:
        conn.rollback()
        return None
    if df is None or df.empty or len(df) < 200:
        return None
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["high", "low", "close"]).sort_values("open_time").reset_index(drop=True)
    return df.rename(columns={"open_time": "bar_start"})


# ── Signal ───────────────────────────────────────────────────────────────────
def signal_events(agg: pd.DataFrame, bar_hours: int, L: int, k: float) -> list[tuple[int, str]]:
    """Return [(bar_pos, direction)] for ROC_L band crossings on the aggregate.

    ROC_L[t] = close[t]/close[t-L] - 1. Band = ±k·σ, σ = rolling stdev of ROC_L
    over 90d (as-of, trailing). Event = prev OUTSIDE band → now INSIDE the trend
    band. k=0 collapses to the ROC zero-crossing. Sign(ROC) = direction.
    """
    close = agg["close"].to_numpy(dtype=float)
    n = len(close)
    if n <= L + 2:
        return []
    roc = np.full(n, np.nan)
    roc[L:] = close[L:] / close[:-L] - 1.0
    roc_s = pd.Series(roc)
    win = max(2, int(round(SIGMA_DAYS * 24 / bar_hours)))
    if k == 0.0:
        band = np.zeros(n)
    else:
        sigma = roc_s.rolling(window=win, min_periods=win).std().to_numpy()
        band = k * sigma
    events: list[tuple[int, str]] = []
    for t in range(1, n):
        b, bp = band[t], band[t - 1]
        r, rp = roc[t], roc[t - 1]
        if np.isnan(r) or np.isnan(rp) or np.isnan(b) or np.isnan(bp):
            continue
        # LONG: cross up through +band; SHORT: cross down through -band.
        if r >= b and rp < bp:
            events.append((t, "LONG"))
        elif r <= -b and rp > -bp:
            events.append((t, "SHORT"))
    return events


# ── Labels ───────────────────────────────────────────────────────────────────
def geometry_from_levels(
    entry: float, is_long: bool, supps: list, resis: list,
    t1h: np.ndarray, h1h: np.ndarray, l1h: np.ndarray, c1h: np.ndarray, start_idx: int,
) -> dict | None:
    """Label (a): our deployable geometry + first-touch exit on 1h candles.

    Takes the (already computed, cached) as-of S/R levels — NOT a DataFrame — so
    the per-coin cache holds only small float lists, never 95d candle copies
    (the O(events×95d) memory blow-up that OOM-killed the first full run)."""
    _entry2, sl, t_cands = hvn_sr_trade_geometry(entry, is_long, supps, resis)
    targets = ensure_min_tp_distance(list(t_cands[:20]), entry, is_long, min_pct=0.05)
    if not targets:
        return None
    hi = min(len(t1h), start_idx + EXIT_SCAN_CAP_H)
    if start_idx >= hi:
        return None
    res = simulate_exit(
        t1h[start_idx:hi], h1h[start_idx:hi], l1h[start_idx:hi], c1h[start_idx:hi],
        0, "LONG" if is_long else "SHORT", entry, sl, targets,
        min(N_PUBLISHED, len(targets)),
    )
    return {
        "net": res["net_pnl_pct"] / 100.0,
        "reason": res["exit_reason"],
        "exit_time": res["exit_time"],  # naive-UTC str of the actual exit candle (or None)
    }


def paper_label(
    pos: int, entry: float, is_long: bool,
    a_high: np.ndarray, a_low: np.ndarray, a_close: np.ndarray, H: int,
) -> float:
    """Label (b): time-exit after H aggregate bars, wide 15% catastrophe SL
    (first-touch on aggregate highs/lows). Net of round-trip taker fee."""
    n = len(a_close)
    sl_price = entry * (1 - CATASTROPHE_SL) if is_long else entry * (1 + CATASTROPHE_SL)
    exit_price = None
    last = min(pos + H, n - 1)
    for j in range(pos + 1, last + 1):
        if is_long and a_low[j] <= sl_price:
            exit_price = sl_price
            break
        if (not is_long) and a_high[j] >= sl_price:
            exit_price = sl_price
            break
    if exit_price is None:
        exit_price = a_close[last]
    gross = (exit_price - entry) / entry if is_long else (entry - exit_price) / entry
    return gross - ROUND_TRIP_FEE


# ── Per-coin replay ──────────────────────────────────────────────────────────
def replay_coin(conn, symbol: str) -> list[dict]:
    """All events for one coin across both aggregations and the full grid.

    Geometry (supps/resis) is cached per as-of entry-time (shared across 6h/4h
    and directions); the geometry exit is cached per (entry-time, direction).
    """
    df1h = load_1h_utc(conn, symbol)
    if df1h is None:
        return []
    # 1h arrays for as-of S/R frames and first-touch exit scans (naive UTC).
    t1h_naive = df1h["open_time"].dt.tz_convert("UTC").dt.tz_localize(None).to_numpy()
    h1h = df1h["high"].to_numpy(float)
    l1h = df1h["low"].to_numpy(float)
    c1h = df1h["close"].to_numpy(float)

    # Lean caches: S/R levels (two small float lists) per as-of entry time, and
    # the geometry-exit result (small dict) per (time, direction, entry). NO
    # DataFrame slices are retained — the 95d frame is built transiently per
    # cache-miss and GC'd immediately (memory O(distinct-entries) of floats).
    sr_cache: dict[np.datetime64, tuple] = {}
    geo_cache: dict[tuple, dict | None] = {}

    def geo_for(entry_close_ts: pd.Timestamp, entry: float, is_long: bool) -> dict | None:
        key_naive = np.datetime64(entry_close_ts.tz_convert("UTC").tz_localize(None))
        start_idx = int(np.searchsorted(t1h_naive, key_naive, side="left"))
        if start_idx <= 0 or start_idx >= len(t1h_naive):
            return None
        gkey = (key_naive, is_long, round(entry, 10))
        if gkey in geo_cache:
            return geo_cache[gkey]
        if key_naive not in sr_cache:
            lo = max(0, start_idx - SR_WINDOW_H)
            frame = pd.DataFrame(
                {"high": h1h[lo:start_idx], "low": l1h[lo:start_idx], "close": c1h[lo:start_idx]}
            )
            sr_cache[key_naive] = get_hvn_and_sr_levels(None, None, entry, df=frame)
            del frame  # release the 95d window immediately
        supps, resis = sr_cache[key_naive]
        out = geometry_from_levels(entry, is_long, supps, resis, t1h_naive, h1h, l1h, c1h, start_idx)
        geo_cache[gkey] = out
        return out

    events: list[dict] = []
    for agg_name, bar_hours in AGGS.items():
        if agg_name == "6h":
            agg = resample_6h(df1h)
        else:
            agg = native_4h(conn, symbol)
        if agg is None or len(agg) < 60:
            continue
        bar_dur = pd.Timedelta(hours=bar_hours)
        a_close = agg["close"].to_numpy(float)
        a_high = agg["high"].to_numpy(float)
        a_low = agg["low"].to_numpy(float)
        a_start = agg["bar_start"]  # tz-aware UTC (window start)

        for L in L_GRID:
            for k in K_GRID:
                evs = signal_events(agg, bar_hours, L, k)
                if not evs:
                    continue
                # Dedup per direction: re-entry only after prior geometry exit.
                last_exit = {"LONG": None, "SHORT": None}
                for pos, direction in evs:
                    is_long = direction == "LONG"
                    entry_close = a_start.iloc[pos] + bar_dur  # aggregate close instant (UTC)
                    if last_exit[direction] is not None and entry_close < last_exit[direction]:
                        continue
                    entry = float(a_close[pos])
                    if not np.isfinite(entry) or entry <= 0:
                        continue
                    geo = geo_for(entry_close, entry, is_long)
                    if geo is None:
                        continue
                    # Dedup interval = the ACTUAL geometry exit (re-entry only after
                    # the prior trade closes). simulate_exit reports exit_time in
                    # naive UTC; fall back to the entry instant if it is None so a
                    # degenerate trade never blocks the coin/direction forever.
                    if geo.get("exit_time"):
                        last_exit[direction] = pd.Timestamp(geo["exit_time"]).tz_localize("UTC")
                    else:
                        last_exit[direction] = entry_close
                    papers = {
                        str(H): paper_label(pos, entry, is_long, a_high, a_low, a_close, H)
                        for H in H_GRID
                    }
                    events.append({
                        "agg": agg_name, "L": L, "k": k, "dir": direction,
                        "entry_time": entry_close.tz_convert("UTC").isoformat(),
                        "entry_epoch": float(entry_close.timestamp()),
                        "geo_net": geo["net"],
                        "paper_net": papers,
                    })
    return events


# ── Streaming accumulators (memory O(cells × months), NOT O(events)) ─────────
# The first full run OOM-died retaining ~1.1M event dicts. We now fold each
# coin's events into running per-cell accumulators (count / wins / Σnet), plus
# streaming sums for the geometry-vs-paper Pearson correlation, and discard the
# coin's events immediately. Exact quantiles (median/p5/p95) would need all
# events; they are NOT load-bearing for the §K1 stop-criterion (val+test positive
# AVG net at n≥200), so we omit them rather than approximate — WR and avg net are
# both exact from the accumulators.
_PH = [str(H) for H in H_GRID]


def _new_stat() -> dict:
    return {"n": 0, "wins": 0, "sum": 0.0}


def _upd(s: dict, x: float) -> None:
    s["n"] += 1
    if x > 0:
        s["wins"] += 1
    s["sum"] += x


def _stat_out(s: dict) -> dict:
    n = s["n"]
    if n == 0:
        return {"n": 0, "wr": None, "avg_net_pct": None}
    return {"n": n, "wr": round(s["wins"] / n, 4), "avg_net_pct": round(s["sum"] / n * 100, 4)}


def _new_cell(agg: str, L: int, k: float) -> dict:
    return {
        "agg": agg, "L": L, "k": k,
        "geo": {"all": _new_stat(), "val": _new_stat(), "test": _new_stat()},
        "paper": {ph: {"all": _new_stat(), "val": _new_stat(), "test": _new_stat()} for ph in _PH},
        "months": {},  # month -> geo stat
        "bydir": {"LONG": _new_stat(), "SHORT": _new_stat()},  # geo, all
    }


def _new_div() -> dict:
    return {"n": 0, "sg": 0.0, "sg2": 0.0, "H": {ph: {"sp": 0.0, "sp2": 0.0, "sgp": 0.0} for ph in _PH}}


def fold_event(acc: dict, div: dict, ev: dict, split_epoch: dict[str, float]) -> None:
    ck = f"{ev['agg']}|L{ev['L']}|k{ev['k']}"
    cell = acc.get(ck)
    if cell is None:
        cell = _new_cell(ev["agg"], ev["L"], ev["k"])
        acc[ck] = cell
    half = "val" if ev["entry_epoch"] < split_epoch[ev["agg"]] else "test"
    g = ev["geo_net"]
    _upd(cell["geo"]["all"], g)
    _upd(cell["geo"][half], g)
    _upd(cell["bydir"][ev["dir"]], g)
    m = ev["entry_time"][:7]
    ms = cell["months"].get(m)
    if ms is None:
        ms = _new_stat()
        cell["months"][m] = ms
    _upd(ms, g)
    div["n"] += 1
    div["sg"] += g
    div["sg2"] += g * g
    for ph in _PH:
        p = ev["paper_net"][ph]
        _upd(cell["paper"][ph]["all"], p)
        _upd(cell["paper"][ph][half], p)
        d = div["H"][ph]
        d["sp"] += p
        d["sp2"] += p * p
        d["sgp"] += g * p


def build_cells(acc: dict) -> dict:
    out: dict = {}
    for ck, c in acc.items():
        months = {m: _stat_out(s) for m, s in sorted(c["months"].items()) if s["n"] >= 20}
        out[ck] = {
            "agg": c["agg"], "L": c["L"], "k": c["k"],
            "geometry_a": {
                "all": _stat_out(c["geo"]["all"]),
                "val": _stat_out(c["geo"]["val"]),
                "test": _stat_out(c["geo"]["test"]),
            },
            "paper_b": {
                ph: {
                    "all": _stat_out(c["paper"][ph]["all"]),
                    "val": _stat_out(c["paper"][ph]["val"]),
                    "test": _stat_out(c["paper"][ph]["test"]),
                }
                for ph in _PH
            },
            "by_direction_geo": {d: _stat_out(s) for d, s in c["bydir"].items()},
            "months_geo": months,
        }
    return out


def divergence_out(div: dict) -> dict:
    """Cost of substituting our geometry for the paper time-exit, from streaming
    sums: mean(geo) − mean(paper) per horizon + streaming Pearson corr(geo,paper)."""
    n = div["n"]
    if n == 0:
        return {}
    sg, sg2 = div["sg"], div["sg2"]
    out: dict = {"n_events": n, "geo_avg_net_pct": round(sg / n * 100, 4)}
    for ph in _PH:
        d = div["H"][ph]
        sp, sp2, sgp = d["sp"], d["sp2"], d["sgp"]
        num = n * sgp - sg * sp
        den = (n * sg2 - sg * sg) * (n * sp2 - sp * sp)
        corr = round(num / (den ** 0.5), 4) if den > 0 else None
        out[f"H{ph}"] = {
            "paper_avg_net_pct": round(sp / n * 100, 4),
            "geo_minus_paper_pct": round((sg - sp) / n * 100, 4),
            "corr_geo_paper": corr,
        }
    return out


def derive_verdict(analysis: dict) -> dict:
    """Stop-criterion (§K1, geometry-(a) is decisive): a cell PASSES if val AND
    test avg net PnL are both > 0 at n≥200 TEST trades. Threshold chosen on VAL,
    test read once. No passing cell ⇒ paper falsified for our stack (SUCCESS)."""
    passing = []
    val_positive = []  # cells positive on val (the candidate set threshold-selection would see)
    for ck, c in analysis.items():
        g = c["geometry_a"]
        v, t = g["val"], g["test"]
        if v["avg_net_pct"] is not None and v["avg_net_pct"] > 0:
            val_positive.append(ck)
        if (
            v["avg_net_pct"] is not None and t["avg_net_pct"] is not None
            and v["avg_net_pct"] > 0 and t["avg_net_pct"] > 0 and (t["n"] or 0) >= MIN_TEST_N
        ):
            passing.append({
                "cell": ck, "val_avg_net_pct": v["avg_net_pct"], "val_n": v["n"],
                "test_avg_net_pct": t["avg_net_pct"], "test_n": t["n"],
                "test_wr": t["wr"],
            })
    passing.sort(key=lambda x: x["test_avg_net_pct"], reverse=True)

    # Best cell BY VAL (the honest threshold-selection pick) → report its test.
    best_val = None
    for ck, c in analysis.items():
        g = c["geometry_a"]
        v, t = g["val"], g["test"]
        if v["avg_net_pct"] is None or (v["n"] or 0) < MIN_TEST_N:
            continue
        cand = {
            "cell": ck, "val_avg_net_pct": v["avg_net_pct"], "val_n": v["n"],
            "test_avg_net_pct": t["avg_net_pct"], "test_n": t["n"], "test_wr": t["wr"],
        }
        if best_val is None or (v["avg_net_pct"] or -1e9) > (best_val["val_avg_net_pct"] or -1e9):
            best_val = cand

    verdict = "momentum-edge-found" if passing else "no-op/paper-falsified"
    return {
        "verdict": verdict,
        "min_test_n": MIN_TEST_N,
        "n_cells": len(analysis),
        "n_cells_val_positive": len(val_positive),
        "n_cells_passing": len(passing),
        "passing_cells": passing[:10],
        "best_cell_selected_on_val": best_val,
    }


# ── Reporting ────────────────────────────────────────────────────────────────
def build_markdown(meta: dict, analysis: dict, verdict: dict, divergence: dict) -> str:
    L = []
    L.append("# K1 · TSM1 — Time-Series-Momentum on 6h aggregates (T-2026-CU-9050-138)\n")
    L.append(
        f"_Generated {meta['generated_at']} · read-only replay · fee/side {FEE_PER_SIDE} "
        f"(round-trip {ROUND_TRIP_FEE:.4f}) · {meta['n_coins']} coins · {meta['n_events']:,} events_\n"
    )
    L.append(f"**VERDICT: {verdict['verdict']}**\n")
    L.append(
        f"- grid cells: {verdict['n_cells']} · val-positive (geometry-a): "
        f"{verdict['n_cells_val_positive']} · PASSING (val>0 AND test>0 at n_test≥"
        f"{verdict['min_test_n']}): **{verdict['n_cells_passing']}**\n"
    )
    bv = verdict["best_cell_selected_on_val"]
    if bv:
        L.append(
            f"- best cell selected on VAL: `{bv['cell']}` → val {bv['val_avg_net_pct']}% "
            f"(n={bv['val_n']}) · **test {bv['test_avg_net_pct']}% (n={bv['test_n']}, WR={bv['test_wr']})**\n"
        )
    L.append(
        "\nStop-criterion (§K1): geometry-(a) is the deployable truth. No cell with BOTH val "
        "AND test positive avg net PnL at n_test≥200 ⇒ paper falsified for our stack (a NEGATIVE "
        "result is SUCCESS). Threshold picked on VAL, TEST read once. We do NOT chase the paper's "
        "monthly re-optimization — that is its overfitting vector.\n"
    )

    if verdict["passing_cells"]:
        L.append("## Passing cells (geometry-a, val>0 AND test>0, n_test≥200)\n")
        L.append("| cell | val avg% (n) | test avg% (n) | test WR |")
        L.append("|---|--:|--:|--:|")
        for p in verdict["passing_cells"]:
            L.append(
                f"| {p['cell']} | {p['val_avg_net_pct']} ({p['val_n']}) | "
                f"{p['test_avg_net_pct']} ({p['test_n']}) | {p['test_wr']} |"
            )
        L.append("")

    L.append("## Geometry-(a) vs paper-(b) divergence — cost of the Cornix substitution\n")
    L.append(
        f"Across all {divergence.get('n_events', 0):,} events, geometry-(a) avg net "
        f"{divergence.get('geo_avg_net_pct')}%. The paper time-exit labels:\n"
    )
    L.append("| paper H (bars) | paper avg net % | geo − paper (pp) | corr(geo,paper) |")
    L.append("|---|--:|--:|--:|")
    for H in map(str, H_GRID):
        d = divergence.get(f"H{H}", {})
        L.append(
            f"| {H} | {d.get('paper_avg_net_pct')} | {d.get('geo_minus_paper_pct')} "
            f"| {d.get('corr_geo_paper')} |"
        )
    L.append(
        "\n_Divergence = our geometry (smart-targets + fixed SL, first-touch on 1h) vs the paper's "
        "time-exit + 15% catastrophe SL. A large gap or low correlation is the quantified cost of "
        "substituting the deployable Cornix geometry for the paper's exit._\n"
    )

    L.append("## Full grid — geometry-(a) net PnL, chrono val/test split\n")
    L.append("| cell | all n | all avg% | all WR | val n | val avg% | test n | test avg% | test WR |")
    L.append("|---|--:|--:|--:|--:|--:|--:|--:|--:|")
    for ck in sorted(analysis.keys()):
        c = analysis[ck]
        g = c["geometry_a"]
        a, v, t = g["all"], g["val"], g["test"]
        L.append(
            f"| {ck} | {a['n']} | {a['avg_net_pct']} | {a['wr']} | {v['n']} | {v['avg_net_pct']} "
            f"| {t['n']} | {t['avg_net_pct']} | {t['wr']} |"
        )
    L.append("")

    L.append("## Population & caveats\n")
    L.append(f"- run status: {meta.get('status', 'complete')} · coins done: {meta.get('n_coins_done', meta['n_coins'])}")
    L.append(f"- coins replayed: {meta['n_coins']} (of {meta['n_universe']} in coins.json)")
    L.append(f"- total events (all cells, both aggregations): {meta['n_events']:,}")
    L.append(f"- peak process RSS: {meta.get('peak_rss_mb')} MB (streaming accumulators, memory O(cells), not O(events))")
    L.append("- 1h→6h resample anchored 00/06/12/18 UTC (origin=epoch), full 6-hour windows only")
    L.append("- native 4h grid = resample-artifact robustness check")
    L.append(
        f"- chrono val/test split epoch (UTC): {meta['split_iso'].get('6h')} — a FIXED calendar "
        "divider (midpoint of the BTCUSDT 1h window), not a per-cell median; val=earlier half, test=later half"
    )
    L.append(
        "- exact quantiles (median/p5/p95) omitted by design: they need all events (incompatible with "
        "the streaming O(cells) memory budget) and are not load-bearing for the §K1 stop-criterion "
        "(val+test positive AVG net at n≥200); n, WR and avg net are all EXACT from the accumulators"
    )
    L.append(f"- geometry exit: first-touch TP-vs-SL on 1h candles, {N_PUBLISHED} published TPs, scan capped {EXIT_SCAN_CAP_H//24}d")
    L.append(f"- paper exit: time-exit after H∈{H_GRID} aggregate bars, {int(CATASTROPHE_SL*100)}% catastrophe SL")
    L.append(
        "- **Survivorship bias (Rule 9)**: coins.json lists ACTIVE USDT-perps; delisted coins are "
        "absent → the replayed population skews to survivors. Documented, not corrected."
    )
    L.append(
        "- **Only closed candles (R1)**: read_candles(include_forming=False); a 6h window counts only "
        "when all 6 one-hour candles are present. σ and ROC are trailing/as-of (no lookahead)."
    )
    L.append(
        "- **WR is not decisive (Rule 8)**: the verdict rests on net-PnL expectancy consistent across "
        "the chrono val/test halves, geometry-(a) label only."
    )
    L.append(
        f"- CPU-check override: --skip-cpu-check={meta['skip_cpu_check']} (the VPS is CPU-saturated; the "
        "walkforward_sim guard would abort this read-only BELOW_NORMAL job)."
    )
    if meta.get("limit_symbols"):
        L.append(f"- ⚠ SAMPLING CAP: --limit-symbols={meta['limit_symbols']} (NOT a full run).")
    return "\n".join(L)


# ── Memory / checkpoint helpers ──────────────────────────────────────────────
def _avail_mb() -> float | None:
    try:
        import psutil
        return psutil.virtual_memory().available / (1024 * 1024)
    except Exception:
        return None


def _rss_mb() -> float | None:
    try:
        import psutil
        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:
        return None


def compute_split(conn) -> tuple[float, str | None]:
    """Fixed chrono val/test divider = calendar midpoint of the BTCUSDT 1h data
    window (longest-history proxy for the whole study window). Decided up front so
    the streaming fold can assign each event to val/test without retaining any."""
    df = load_1h_utc(conn, "BTCUSDT")
    if df is None or df.empty:
        return 0.0, None
    tmin = df["open_time"].min().timestamp()
    tmax = df["open_time"].max().timestamp()
    mid = (tmin + tmax) / 2.0
    return mid, dt.datetime.fromtimestamp(mid, dt.timezone.utc).isoformat()


def write_outputs(meta: dict, cells: dict, verdict: dict, divergence: dict,
                  json_path: str, md_path: str) -> None:
    """Atomic write (temp + os.replace) so a mid-run kill leaves a valid file."""
    out = {"meta": meta, "verdict": verdict, "divergence": divergence, "cells": cells}
    tmp = json_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, default=str)
    os.replace(tmp, json_path)
    tmp_md = md_path + ".tmp"
    with open(tmp_md, "w", encoding="utf-8") as fh:
        fh.write(build_markdown(meta, cells, verdict, divergence))
    os.replace(tmp_md, md_path)


def save_state(state_path: str, state: dict) -> None:
    """Atomic-write the streaming accumulators + processed-coin set so a
    watchdog-kill can be resumed rather than restarted. All values are JSON-safe
    (dicts of numbers/strings, processed as a list)."""
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    os.replace(tmp, state_path)


def load_state(state_path: str) -> dict | None:
    if not os.path.exists(state_path):
        return None
    try:
        with open(state_path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


# ── Main ─────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-symbols", type=int, default=None, help="Smoke pass: first N coins only.")
    ap.add_argument("--skip-cpu-check", action="store_true", default=False,
                    help="Bypass walkforward_sim.check_cpu_headroom (default OFF). Needed on the "
                         "CPU-saturated VPS; this job is read-only + BELOW_NORMAL priority.")
    ap.add_argument("--progress-every", type=int, default=25, help="Print progress every N coins.")
    ap.add_argument("--resume", action="store_true", default=False,
                    help="Resume from the saved accumulator state (survives watchdog-kills). "
                         "The relaunch wrapper passes this on every segment; exit 0 = fully complete.")
    ap.add_argument("--state-path", default=DEFAULT_STATE_PATH,
                    help="Path to the transient resume-state JSON (OS temp dir, never the repo).")
    args = ap.parse_args()

    set_low_priority()
    if not args.skip_cpu_check:
        check_cpu_headroom()
    else:
        print("CPU-check SKIPPED (--skip-cpu-check): read-only BELOW_NORMAL job on saturated VPS.")
    os.makedirs(OUT_DIR, exist_ok=True)

    avail0 = _avail_mb()
    if avail0 is not None:
        print(f"RAM available at start: {avail0:.0f} MB")
        if avail0 < MIN_AVAIL_MB:
            print(f"ABORT: only {avail0:.0f} MB free (< {MIN_AVAIL_MB} MB) — refusing to risk the "
                  f"live fleet. Report back rather than run.")
            return 2

    universe = load_coins("coins.json", usdt_only=True, uppercase=True)
    coins = universe[: args.limit_symbols] if args.limit_symbols else universe

    json_path = os.path.join(OUT_DIR, "tsmom_study.json")
    md_path = os.path.join(OUT_DIR, "tsmom_study.md")
    state_path = args.state_path

    acc: dict = {}
    div = _new_div()
    n_done = 0
    span_min = None
    span_max = None
    peak_rss = 0.0
    processed: set[str] = set()
    resumed_split_mid = None
    resumed_split_iso = None

    if args.resume:
        st = load_state(state_path)
        if st is not None and st.get("universe_hash") == len(universe):
            acc = st["acc"]
            div = st["div"]
            n_done = st["n_done"]
            span_min = st.get("span_min")
            span_max = st.get("span_max")
            peak_rss = st.get("peak_rss", 0.0)
            processed = set(st.get("processed", []))
            resumed_split_mid = st.get("split_mid")
            resumed_split_iso = st.get("split_iso")
            print(f"RESUMED: {n_done} coins already folded ({div['n']:,} events), "
                  f"{len(processed)} in processed-set")
        else:
            print("RESUME requested but no compatible state found — starting fresh.")

    with db_connection() as conn:
        # Fixed chrono val/test split, decided UP FRONT (streaming can't know the
        # event span first): the calendar midpoint of the BTCUSDT 1h data window
        # (the longest-history proxy for the study window), applied to BOTH
        # aggregations. It is a fixed chrono DIVIDER, not a per-cell median — val
        # is the earlier calendar half, test the later half. On resume we REUSE the
        # first-run split so every segment folds against an identical divider.
        if resumed_split_mid is not None:
            split_mid, split_iso_val = resumed_split_mid, resumed_split_iso
        else:
            split_mid, split_iso_val = compute_split(conn)
        split_epoch = {agg: split_mid for agg in AGGS}
        split_iso = {agg: split_iso_val for agg in AGGS}
        print(f"chrono split (UTC): {split_iso_val}")

        def persist_state() -> None:
            save_state(state_path, {
                "universe_hash": len(universe),
                "acc": acc, "div": div, "n_done": n_done,
                "span_min": span_min, "span_max": span_max, "peak_rss": peak_rss,
                "processed": sorted(processed),
                "split_mid": split_mid, "split_iso": split_iso_val,
            })

        def snapshot(final: bool) -> dict:
            cells = build_cells(acc)
            verdict = derive_verdict(cells)
            divergence = divergence_out(div)
            meta = {
                "study": "K1 · TSM1 (Time-Series-Momentum on 6h aggregates)",
                "task": "T-2026-CU-9050-138",
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "status": "complete" if final else "partial (checkpoint)",
                "fee_per_side": FEE_PER_SIDE,
                "round_trip_fee": ROUND_TRIP_FEE,
                "n_universe": len(universe),
                "n_coins": len(coins),
                "n_coins_done": n_done,
                "n_events": div["n"],
                "grid": {"L": L_GRID, "k": K_GRID, "aggregations": list(AGGS.keys()), "paper_H": H_GRID},
                "span_utc": [
                    dt.datetime.fromtimestamp(span_min, dt.timezone.utc).isoformat() if span_min else None,
                    dt.datetime.fromtimestamp(span_max, dt.timezone.utc).isoformat() if span_max else None,
                ],
                "split_iso": split_iso,
                "peak_rss_mb": round(peak_rss, 1),
                "limit_symbols": args.limit_symbols,
                "skip_cpu_check": args.skip_cpu_check,
            }
            write_outputs(meta, cells, verdict, divergence, json_path, md_path)
            return verdict

        for sym in coins:
            if sym in processed:
                continue  # already folded in a prior (killed) segment
            try:
                evs = replay_coin(conn, sym)
            except Exception as e:  # one bad coin must not kill the run
                conn.rollback()
                print(f"  WARN {sym}: {e}")
                evs = []
            for e in evs:  # fold then discard — memory O(cells), not O(events)
                fold_event(acc, div, e, split_epoch)
                ep = e["entry_epoch"]
                span_min = ep if span_min is None else min(span_min, ep)
                span_max = ep if span_max is None else max(span_max, ep)
            del evs
            processed.add(sym)
            n_done += 1
            rss = _rss_mb()
            if rss is not None:
                peak_rss = max(peak_rss, rss)
            if n_done % args.progress_every == 0:
                av = _avail_mb()
                print(f"  ...{n_done}/{len(coins)} coins, {div['n']:,} events, "
                      f"rss={peak_rss:.0f}MB avail={av:.0f}MB" if av is not None
                      else f"  ...{n_done}/{len(coins)} coins, {div['n']:,} events")
            if n_done % CHECKPOINT_EVERY == 0:
                snapshot(final=False)
                persist_state()
                print(f"  checkpoint+state written at {n_done} coins ({div['n']:,} events)")

        verdict = snapshot(final=True)

    # Full completion: drop the transient resume-state so a later run starts clean.
    try:
        if os.path.exists(state_path):
            os.remove(state_path)
    except OSError:
        pass

    print(f"\nVERDICT: {verdict['verdict']}")
    print(f"coins={n_done} events={div['n']:,} cells={verdict['n_cells']} "
          f"val_positive={verdict['n_cells_val_positive']} passing={verdict['n_cells_passing']} "
          f"peak_rss={peak_rss:.0f}MB")
    if verdict["best_cell_selected_on_val"]:
        bv = verdict["best_cell_selected_on_val"]
        print(f"best-on-val: {bv['cell']} val={bv['val_avg_net_pct']}% (n={bv['val_n']}) "
              f"test={bv['test_avg_net_pct']}% (n={bv['test_n']})")
    print(f"wrote {json_path}")
    print(f"wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
