#!/usr/bin/env py -3.13
# tools/mps1_event_study.py — MPS1 max-pain band-touch event study (T-2026-KYT-9050-073)
"""Edge GATE for the MartyParty "trade the spread between 24h futures Max
Pains" idea, BEFORE any trade backtest is built: do touches of estimated
liquidation clusters (tools/mps1_liq_heatmap.py) carry mean-reversion edge
beyond generic extreme-reversion?

EVENTS (per symbol, only CLOSED 5m candles — Rule 5; decision uses the band
of the PREVIOUS bar, strictly as-of):
  * UP touch:   high[t] ≥ upper_band[t−1], band ≥ MIN_EVENT_BAND_DIST above
                close[t−1]. Reversion direction = SHORT from close[t].
  * DOWN touch: low[t] ≤ lower_band[t−1] (mirror, LONG from close[t]).
  * Per-side cooldown of COOLDOWN_BARS after each event (overlap control).

CONTROLS (isolate "cluster magnetism" from plain extreme-reversion):
  * UP control:   high[t] is a fresh trailing-24h high AND no upper band
                  within CONTROL_EXCLUSION_DIST of it (band absent or far).
  * DOWN control: mirror on fresh 24h lows. Same cooldown discipline.

OUTCOMES per event/control (measured from close[t], forward-only):
  * Reversion returns at 1h/4h/8h/24h, signed in the reversion direction
    (gross; net = gross − round-trip taker fee, Rule 10 / FEE_PER_SIDE).
  * SPREAD CAPTURE (events only, needs BOTH bands at t−1): first-touch scan
    over the next 24h — does price reach the OPPOSITE band before breaching
    the touched band by an SL tolerance ∈ {0.5 %, 1 %, 2 %}? Both in one bar
    → counted as LOSS (conservative first-touch, house convention). Timeout
    → marked-to-market at the scan end. PnL net of round-trip fee.

SPLIT: one GLOBAL chrono midpoint of the oi_5m span (val = first half,
test = second half; event assigned by its bar close). VERDICT (pre-registered,
Rule 8 — expectancy over WR): the gate PASSES for a side iff at the 4h horizon
  net mean > 0  AND  event gross mean > control gross mean
hold on VAL and TEST with n ≥ MIN_CELL_N events per half. No passing side ⇒
NO-EDGE ⇒ MPS1 is falsified for our stack and gets parked — a negative result
is a SUCCESS (K11/WSH1 discipline), do NOT force a positive.

Density buckets, OI tiers and SL tolerances are DESCRIPTIVE slices (reported,
not part of the gate) — with only ~7 weeks of OI history a per-slice gate
would be pure multiple-comparison bait.

Honest limits (also in the report): ~7 weeks of oi_5m history = ONE market
regime, in-sample only (T-007 lesson); the heatmap's leverage mix is an
assumption (see engine header); survivorship — coins.json is active perps
only (documented, not corrected).

READ-ONLY: SELECTs only, BELOW_NORMAL priority, sole job on a real-money VPS
(sequential-jobs rule). Artifacts → staging_models/ ONLY (Rule 2).

Usage:
  python tools/mps1_event_study.py                  # full universe
  python tools/mps1_event_study.py --limit-symbols 40
  python tools/mps1_event_study.py --dump-symbol BTCUSDT   # eyeball bands
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

from core.candles import read_candles  # noqa: E402
from core.database import db_connection  # noqa: E402
from core.market_utils import load_coins  # noqa: E402
from core.time import epoch_seconds  # noqa: E402
from tools.mps1_liq_heatmap import HeatmapConfig, build_heatmap  # noqa: E402
from tools.walkforward_sim import (  # noqa: E402
    FEE_PER_SIDE,
    check_cpu_headroom,
    set_low_priority,
)

TF = "5m"
ROUND_TRIP_FEE = 2.0 * FEE_PER_SIDE  # 0.10 % — netted off every reported "net" figure

WARMUP_BARS = 288  # first 24h only warm the heatmap, no events
HORIZONS: dict[str, int] = {"1h": 12, "4h": 48, "8h": 96, "24h": 288}
VERDICT_HORIZON = "4h"
SL_TOLERANCES: tuple[float, ...] = (0.005, 0.01, 0.02)
SPREAD_SCAN_CAP = 288  # 24h — the daily-reset spirit of the source strategy
COOLDOWN_BARS = 48  # 4h between same-side events (overlap control)
MIN_EVENT_BAND_DIST = 0.003  # band ≥ 0.3 % away from prior close, else noise
CONTROL_EXCLUSION_DIST = 0.005  # control high/low must be ≥ 0.5 % from any band
MIN_BARS = 2 * WARMUP_BARS  # skip symbols with less closed-candle history
MIN_CELL_N = 200  # gate floor per (side, half)

DENSITY_BUCKETS: tuple[tuple[str, float, float], ...] = (
    ("d<10%", 0.0, 0.10),
    ("d10-25%", 0.10, 0.25),
    ("d>=25%", 0.25, 1.01),
)
OI_TIERS: tuple[tuple[str, float], ...] = (  # by the symbol's MEAN oi_value_usdt
    ("mega>=1B", 1e9),
    ("major>=100M", 1e8),
    ("mid>=10M", 1e7),
    ("tail", 0.0),
)

JSON_PATH = os.path.join("staging_models", "mps1_event_study.json")
MD_PATH = os.path.join("staging_models", "mps1_event_study.md")


# ── Accumulators (fold events, never keep them — memory O(cells)) ────────────


def _new_stat() -> dict:
    return {"n": 0, "sum": 0.0, "sum2": 0.0}


def _fold_stat(st: dict, x: float) -> None:
    st["n"] += 1
    st["sum"] += x
    st["sum2"] += x * x


def _stat_row(st: dict) -> dict:
    n = st["n"]
    if n == 0:
        return {"n": 0, "mean": None, "tstat": None}
    mean = st["sum"] / n
    var = max(st["sum2"] / n - mean * mean, 0.0)
    t = mean / math.sqrt(var / n) if var > 0 and n > 1 else None
    return {"n": n, "mean": mean, "tstat": t}


def new_acc() -> dict:
    """acc[population][side][half] → per-horizon return stats + slices."""
    acc: dict = {}
    for pop in ("event", "control"):
        acc[pop] = {}
        for side in ("up", "down"):
            acc[pop][side] = {}
            for half in ("val", "test"):
                cell = {
                    "gross": {h: _new_stat() for h in HORIZONS},
                    "net": {h: _new_stat() for h in HORIZONS},
                }
                if pop == "event":
                    cell["density"] = {b[0]: _new_stat() for b in DENSITY_BUCKETS}
                    cell["oi_tier"] = {t[0]: _new_stat() for t in OI_TIERS}
                    cell["spread"] = {
                        f"{tol:.3f}": {"win": 0, "loss": 0, "timeout": 0, "sum_pnl": 0.0} for tol in SL_TOLERANCES
                    }
                    cell["no_opposite_band"] = 0
                acc[pop][side][half] = cell
    return acc


def _density_bucket(frac: float) -> str:
    for name, lo, hi in DENSITY_BUCKETS:
        if lo <= frac < hi:
            return name
    return DENSITY_BUCKETS[-1][0]


def _oi_tier(mean_oi: float) -> str:
    for name, floor in OI_TIERS:
        if mean_oi >= floor:
            return name
    return OI_TIERS[-1][0]


# ── Per-symbol replay ────────────────────────────────────────────────────────


def fetch_symbol(conn, symbol: str, start: dt.datetime):
    candles = read_candles(conn, symbol, TF, start=start, columns=("open_time", "open", "high", "low", "close"))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ts, oi_value_usdt FROM oi_5m WHERE symbol = %s AND ts >= %s ORDER BY ts",
            (symbol, start),
        )
        oi = pd.DataFrame(cur.fetchall(), columns=["ts", "oi_value_usdt"])
    return candles, oi


def replay_symbol(acc: dict, counters: dict, candles, oi, split_epoch: float, cfg: HeatmapConfig) -> None:
    hm = build_heatmap(candles, oi, cfg)
    n = len(hm)
    close = hm["close"].to_numpy()
    high = candles["high"].to_numpy()
    low = candles["low"].to_numpy()
    # epoch_seconds, NOT astype("int64")/1e9 — the astype idiom silently yields
    # kiloseconds on datetime64[us] frames (pandas 3.x), which put EVERY event
    # into the val half on the first full run of this study.
    close_epoch = epoch_seconds(candles["open_time"]) + 300.0

    ub = hm["upper_band"].to_numpy()
    lb = hm["lower_band"].to_numpy()
    ub_frac = hm["upper_density_frac"].to_numpy()
    lb_frac = hm["lower_density_frac"].to_numpy()
    mean_oi = float(np.nanmean(hm["oi_value_usdt"].to_numpy())) if n else 0.0
    tier = _oi_tier(mean_oi)

    # Trailing-24h extremes EXCLUDING the current bar (control definition).
    prior_max = pd.Series(high).rolling(WARMUP_BARS).max().shift(1).to_numpy()
    prior_min = pd.Series(low).rolling(WARMUP_BARS).min().shift(1).to_numpy()

    last_event = {"up": -(10**9), "down": -(10**9)}
    last_control = {"up": -(10**9), "down": -(10**9)}

    for t in range(WARMUP_BARS, n - 1):
        half = "val" if close_epoch[t] < split_epoch else "test"
        pu, pl = ub[t - 1], lb[t - 1]  # bands known at the prior close (as-of)

        for side in ("up", "down"):
            if side == "up":
                band, opp, frac = pu, pl, ub_frac[t - 1]
                touched = not np.isnan(band) and high[t] >= band and band / close[t - 1] - 1.0 >= MIN_EVENT_BAND_DIST
                fresh_extreme = not np.isnan(prior_max[t]) and high[t] >= prior_max[t]
                near_band = not np.isnan(band) and abs(high[t] / band - 1.0) < CONTROL_EXCLUSION_DIST
            else:
                band, opp, frac = pl, pu, lb_frac[t - 1]
                touched = not np.isnan(band) and low[t] <= band and 1.0 - band / close[t - 1] >= MIN_EVENT_BAND_DIST
                fresh_extreme = not np.isnan(prior_min[t]) and low[t] <= prior_min[t]
                near_band = not np.isnan(band) and abs(low[t] / band - 1.0) < CONTROL_EXCLUSION_DIST

            sign = -1.0 if side == "up" else 1.0  # reversion direction from close[t]

            if touched and t - last_event[side] >= COOLDOWN_BARS:
                last_event[side] = t
                cell = acc["event"][side][half]
                counters["n_events"] += 1
                for hname, hbars in HORIZONS.items():
                    if t + hbars < n:
                        r = sign * (close[t + hbars] / close[t] - 1.0)
                        _fold_stat(cell["gross"][hname], r)
                        _fold_stat(cell["net"][hname], r - ROUND_TRIP_FEE)
                if t + HORIZONS[VERDICT_HORIZON] < n:
                    r4 = sign * (close[t + HORIZONS[VERDICT_HORIZON]] / close[t] - 1.0)
                    _fold_stat(cell["density"][_density_bucket(frac)], r4 - ROUND_TRIP_FEE)
                    _fold_stat(cell["oi_tier"][tier], r4 - ROUND_TRIP_FEE)
                if np.isnan(opp):
                    cell["no_opposite_band"] += 1
                else:
                    _fold_spread(cell["spread"], side, t, close, high, low, band, opp, n)

            elif fresh_extreme and not near_band and t - last_control[side] >= COOLDOWN_BARS:
                last_control[side] = t
                cell = acc["control"][side][half]
                counters["n_controls"] += 1
                for hname, hbars in HORIZONS.items():
                    if t + hbars < n:
                        r = sign * (close[t + hbars] / close[t] - 1.0)
                        _fold_stat(cell["gross"][hname], r)
                        _fold_stat(cell["net"][hname], r - ROUND_TRIP_FEE)


def _fold_spread(spread_cell: dict, side: str, t: int, close, high, low, band: float, opp: float, n: int) -> None:
    """First-touch scan close[t] → opposite band vs touched band + tolerance."""
    end = min(n, t + 1 + SPREAD_SCAN_CAP)
    entry = close[t]
    for tol in SL_TOLERANCES:
        sl = band * (1.0 + tol) if side == "up" else band * (1.0 - tol)
        outcome, pnl = "timeout", None
        for j in range(t + 1, end):
            if side == "up":
                hit_tp, hit_sl = low[j] <= opp, high[j] >= sl
            else:
                hit_tp, hit_sl = high[j] >= opp, low[j] <= sl
            if hit_sl:  # SL-first on the ambiguous both-in-one-bar case (conservative)
                outcome = "loss"
                pnl = -abs(sl / entry - 1.0) - ROUND_TRIP_FEE
                break
            if hit_tp:
                outcome = "win"
                pnl = abs(opp / entry - 1.0) - ROUND_TRIP_FEE
                break
        if pnl is None:  # timeout → mark-to-market at scan end
            j_last = end - 1
            sign = -1.0 if side == "up" else 1.0
            pnl = sign * (close[j_last] / entry - 1.0) - ROUND_TRIP_FEE
        st = spread_cell[f"{tol:.3f}"]
        st[outcome] += 1
        st["sum_pnl"] += pnl


# ── Verdict & report ─────────────────────────────────────────────────────────


def derive_verdict(acc: dict) -> dict:
    sides = {}
    for side in ("up", "down"):
        checks = {}
        ok = True
        for half in ("val", "test"):
            ev_net = _stat_row(acc["event"][side][half]["net"][VERDICT_HORIZON])
            ev_gross = _stat_row(acc["event"][side][half]["gross"][VERDICT_HORIZON])
            ct_gross = _stat_row(acc["control"][side][half]["gross"][VERDICT_HORIZON])
            passed = (
                ev_net["n"] >= MIN_CELL_N
                and ev_net["mean"] is not None
                and ev_net["mean"] > 0
                and ct_gross["mean"] is not None
                and ev_gross["mean"] > ct_gross["mean"]
            )
            checks[half] = {
                "event_net": ev_net,
                "event_gross": ev_gross,
                "control_gross": ct_gross,
                "passed": passed,
            }
            ok = ok and passed
        sides[side] = {"checks": checks, "passed": ok}
    passing = [s for s, v in sides.items() if v["passed"]]
    return {
        "verdict": "EDGE" if passing else "NO-EDGE",
        "passing_sides": passing,
        "horizon": VERDICT_HORIZON,
        "min_cell_n": MIN_CELL_N,
        "sides": sides,
    }


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{100.0 * x:+.3f}%"


def _render_returns_table(acc: dict, pop: str, kind: str) -> list[str]:
    lines = [
        f"| side | half | {' | '.join(f'{h} n / mean / t' for h in HORIZONS)} |",
        f"|---|---|{'---|' * len(HORIZONS)}",
    ]
    for side in ("up", "down"):
        for half in ("val", "test"):
            cells = []
            for h in HORIZONS:
                row = _stat_row(acc[pop][side][half][kind][h])
                ttxt = f"{row['tstat']:.1f}" if row["tstat"] is not None else "—"
                cells.append(f"{row['n']} / {_pct(row['mean'])} / {ttxt}")
            lines.append(f"| {side} | {half} | {' | '.join(cells)} |")
    return lines


def write_outputs(meta: dict, acc: dict, verdict: dict) -> None:
    os.makedirs("staging_models", exist_ok=True)
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "verdict": verdict, "acc": acc}, f, indent=2, default=str)

    md: list[str] = [
        "# MPS1 — Max-Pain-Band-Touch Event Study (T-2026-KYT-9050-073)",
        "",
        f"Generated: {meta['generated_at']} · status: {meta['status']}",
        f"Universe: {meta['n_coins_done']}/{meta['n_coins']} symbols · "
        f"events: {meta['n_events']:,} · controls: {meta['n_controls']:,}",
        f"Window: {meta['data_start']} → {meta['data_end']} (split {meta['split_iso']})",
        "",
        f"## VERDICT: **{verdict['verdict']}**"
        + (f" (sides: {', '.join(verdict['passing_sides'])})" if verdict["passing_sides"] else ""),
        "",
        f"Gate ({VERDICT_HORIZON}, per side, val AND test, n ≥ {MIN_CELL_N}): net mean > 0 "
        "AND event gross mean > control gross mean.",
        "",
        "## Event reversion returns (net of 0.10 % round-trip fee)",
        "",
        *_render_returns_table(acc, "event", "net"),
        "",
        "## Event reversion returns (gross)",
        "",
        *_render_returns_table(acc, "event", "gross"),
        "",
        "## Control reversion returns (gross — fresh 24h extremes without a nearby band)",
        "",
        *_render_returns_table(acc, "control", "gross"),
        "",
        f"## Descriptive slices (net {VERDICT_HORIZON} returns; NOT part of the gate)",
        "",
        "| slice | cell | val n / mean | test n / mean |",
        "|---|---|---|---|",
    ]
    for side in ("up", "down"):
        for name, _, _ in DENSITY_BUCKETS:
            v = _stat_row(acc["event"][side]["val"]["density"][name])
            te = _stat_row(acc["event"][side]["test"]["density"][name])
            md.append(f"| density ({side}) | {name} | {v['n']} / {_pct(v['mean'])} | {te['n']} / {_pct(te['mean'])} |")
        for name, _ in OI_TIERS:
            v = _stat_row(acc["event"][side]["val"]["oi_tier"][name])
            te = _stat_row(acc["event"][side]["test"]["oi_tier"][name])
            md.append(f"| oi-tier ({side}) | {name} | {v['n']} / {_pct(v['mean'])} | {te['n']} / {_pct(te['mean'])} |")
    md += [
        "",
        "## Spread capture (events with both bands; first-touch, SL-first on ambiguity, net PnL)",
        "",
        "| side | half | tol | win | loss | timeout | win-rate | mean net PnL |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for side in ("up", "down"):
        for half in ("val", "test"):
            cell = acc["event"][side][half]
            for tol in SL_TOLERANCES:
                st = cell["spread"][f"{tol:.3f}"]
                total = st["win"] + st["loss"] + st["timeout"]
                wr = f"{100.0 * st['win'] / total:.1f}%" if total else "—"
                mp = _pct(st["sum_pnl"] / total) if total else "—"
                md.append(
                    f"| {side} | {half} | {tol:.1%} | {st['win']} | {st['loss']} | {st['timeout']} | {wr} | {mp} |"
                )
            md.append(f"| {side} | {half} | — no opposite band: {cell['no_opposite_band']} | | | | | |")
    md += [
        "",
        "## Honest limits",
        "",
        "* ~7 weeks of oi_5m history = ONE market regime, in-sample only (T-007 lesson).",
        "* Heatmap leverage mix / long-short split are assumptions (see engine header) — "
        "bands are estimates, not ground truth; no liquidation feed to calibrate against.",
        "* Survivorship: coins.json = active USDT perps only (documented, not corrected).",
        "* Overlap: 4h cooldown; horizons beyond 4h still overlap across events.",
        "",
    ]
    with open(MD_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(md))


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description="MPS1 max-pain band-touch event study (read-only)")
    ap.add_argument("--limit-symbols", type=int, default=0, help="cap the universe (0 = all)")
    ap.add_argument("--skip-cpu-check", action="store_true")
    ap.add_argument("--progress-every", type=int, default=20)
    ap.add_argument("--checkpoint-every", type=int, default=50)
    ap.add_argument("--dump-symbol", default="", help="print current bands for one symbol and exit")
    args = ap.parse_args()

    set_low_priority()
    if not args.skip_cpu_check:
        check_cpu_headroom()

    cfg = HeatmapConfig()

    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT min(ts), max(ts) FROM oi_5m")
            oi_min, oi_max = cur.fetchone()
        if oi_min is None:
            print("oi_5m is empty — nothing to study")
            return 1
        split_dt = oi_min + (oi_max - oi_min) / 2
        split_epoch = split_dt.timestamp()

        if args.dump_symbol:
            candles, oi = fetch_symbol(conn, args.dump_symbol, oi_min)
            hm = build_heatmap(candles, oi, cfg)
            print(hm.tail(3).to_string())
            return 0

        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT symbol FROM oi_5m")
            oi_symbols = {r[0] for r in cur.fetchall()}
        coins = [c for c in load_coins(usdt_only=True, uppercase=True) if c in oi_symbols]
        if args.limit_symbols:
            coins = coins[: args.limit_symbols]
        # ASCII-only console output — the VPS console is cp1252 (UnicodeEncodeError trap).
        print(f"universe: {len(coins)} symbols, window {oi_min} -> {oi_max}, split {split_dt}")

        acc = new_acc()
        counters = {"n_events": 0, "n_controls": 0}
        n_done = 0
        skipped: list[str] = []

        def snapshot(final: bool) -> dict:
            verdict = derive_verdict(acc)
            meta = {
                "study": "MPS1 max-pain band-touch event study (5m)",
                "task": "T-2026-KYT-9050-073",
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "status": "complete"
                if final and not args.limit_symbols
                else ("partial (sampling cap)" if final else "partial (checkpoint)"),
                "n_coins": len(coins),
                "n_coins_done": n_done,
                "n_skipped_short_history": len(skipped),
                "n_events": counters["n_events"],
                "n_controls": counters["n_controls"],
                "data_start": str(oi_min),
                "data_end": str(oi_max),
                "split_iso": str(split_dt),
                "fee_per_side": FEE_PER_SIDE,
                "config": {
                    "leverage_tiers": cfg.leverage_tiers,
                    "tier_weights": cfg.tier_weights,
                    "maintenance_margin": cfg.maintenance_margin,
                    "window_bars": cfg.window_bars,
                    "bin_bp": cfg.bin_bp,
                    "smooth_bins": cfg.smooth_bins,
                    "min_band_distance": cfg.min_band_distance,
                    "max_band_distance": cfg.max_band_distance,
                    "warmup_bars": WARMUP_BARS,
                    "cooldown_bars": COOLDOWN_BARS,
                    "min_event_band_dist": MIN_EVENT_BAND_DIST,
                    "control_exclusion_dist": CONTROL_EXCLUSION_DIST,
                    "sl_tolerances": SL_TOLERANCES,
                    "spread_scan_cap": SPREAD_SCAN_CAP,
                },
            }
            write_outputs(meta, acc, verdict)
            return verdict

        for sym in coins:
            try:
                candles, oi = fetch_symbol(conn, sym, oi_min)
                if len(candles) < MIN_BARS or len(oi) < MIN_BARS:
                    skipped.append(sym)
                else:
                    replay_symbol(acc, counters, candles, oi, split_epoch, cfg)
            except Exception as e:  # one bad coin must not kill the run
                conn.rollback()
                print(f"  WARN {sym}: {e}")
            n_done += 1
            if n_done % max(1, args.progress_every) == 0:
                print(
                    f"  ...{n_done}/{len(coins)} symbols, {counters['n_events']:,} events, "
                    f"{counters['n_controls']:,} controls"
                )
            if n_done % max(1, args.checkpoint_every) == 0:
                snapshot(final=False)

        verdict = snapshot(final=True)

    print(f"\nVERDICT: {verdict['verdict']} (passing sides: {verdict['passing_sides'] or 'none'})")
    print(f"events={counters['n_events']:,} controls={counters['n_controls']:,} skipped_short={len(skipped)}")
    print(f"wrote {JSON_PATH}")
    print(f"wrote {MD_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
