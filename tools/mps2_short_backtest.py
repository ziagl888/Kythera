#!/usr/bin/env py -3.13
# tools/mps2_short_backtest.py — MPS2 upper-band SHORT backtest, house geometry (T-2026-KYT-9050-078)
"""Backtest of the ONLY candidate that survived the MPS1 edge gate
(T-2026-KYT-9050-073, staging_models/mps1_event_study.md): SHORT after a touch
of the upper liquidation-cluster band, now under OUR deployable geometry —
the K11/wick_reversal_study wiring (smart targets + fixed SL, first-touch
ladder), NOT the refuted band-to-band spread trade.

Event definition (identical semantics to the gate study, SHORT side only —
constants imported from tools.mps1_event_study so the two cannot drift):
  * band of the PREVIOUS closed 5m bar (strictly as-of, Rule 5),
  * high[t] ≥ upper_band[t−1], band ≥ MIN_EVENT_BAND_DIST above close[t−1],
  * no new entry while the prior trade's geometry exit is still open
    (K11 re-entry convention — replaces the gate study's fixed cooldown,
    because here every event IS a simulated position).

Trade wiring (EXACTLY as tools/wick_reversal_study.py / tsmom_study.py):
  entry = touch-bar CLOSE (5m) → get_hvn_and_sr_levels(df = as-of trailing
  15m frame) → hvn_sr_trade_geometry(SHORT) → ensure_min_tp_distance(5 %) →
  simulate_exit (first-touch TP-vs-SL ladder, N_PUBLISHED=3, taker fees,
  SL-first on same-bar ambiguity), exit scan on the 5m tape capped at
  EXIT_SCAN_CAP_BARS. Strictly as-of: the S/R frame ends before the entry,
  the exit scan starts strictly after the entry bar, NO live lookups.

Verdict (pre-registered, Rule 8 — expectancy over WR): PASS iff val AND test
avg net PnL > 0 at n ≥ MIN_HALF_N events per chrono half (split = oi_5m span
midpoint, epoch_seconds — NOT astype/1e9, the T-073 kilosecond trap). NO
passing ⇒ MPS2 falsified under our geometry — a NEGATIVE result is SUCCESS,
documented and parked (do NOT force a positive).

HONEST FRAMING (in the report too): this runs on the SAME window that
generated the gate verdict — it is an IN-SAMPLE deployability check of the
surviving hypothesis, not fresh evidence (T-007 lesson). Real out-of-sample
needs future oi_5m weeks; the collector keeps buying that window every day.

READ-ONLY: SELECTs only, BELOW_NORMAL priority, sole job on a real-money VPS.
Artifacts → staging_models/ ONLY (Rule 2). Survivorship: coins.json = active
USDT perps only (documented, not corrected).

Usage:
  python tools/mps2_short_backtest.py                  # full universe
  python tools/mps2_short_backtest.py --limit-symbols 40 --skip-cpu-check
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
from core.trade_utils import (  # noqa: E402
    ensure_min_tp_distance,
    get_hvn_and_sr_levels,
    hvn_sr_trade_geometry,
)
from tools.mps1_event_study import (  # noqa: E402
    MIN_EVENT_BAND_DIST,
    WARMUP_BARS,
)
from tools.mps1_liq_heatmap import BAR_SECONDS, HeatmapConfig, build_heatmap  # noqa: E402
from tools.walkforward_sim import (  # noqa: E402
    FEE_PER_SIDE,
    check_cpu_headroom,
    set_low_priority,
    simulate_exit,
)

TF_EVENT = "5m"  # event detection + exit scan tape
TF_SR = "15m"  # as-of S/R frame for the smart targets (K11 convention)
SR_WINDOW_BARS = 30 * 96  # trailing 30 days of 15m candles
MIN_SR_BARS = 50  # get_hvn_and_sr_levels floor
N_PUBLISHED = 3  # published TPs (house reversal convention)
MIN_TP_PCT = 0.05  # ensure_min_tp_distance house default
EXIT_SCAN_CAP_BARS = 4 * 288  # bound the first-touch scan to 4d of 5m bars
MIN_BARS = 2 * WARMUP_BARS  # skip symbols with less 5m history
MIN_HALF_N = 100  # verdict floor per chrono half

JSON_PATH = os.path.join("staging_models", "mps2_short_backtest.json")
MD_PATH = os.path.join("staging_models", "mps2_short_backtest.md")


# ── Accumulators (fold trades, never keep them) ──────────────────────────────


def new_acc() -> dict:
    return {
        half: {
            "n": 0,
            "sum_net": 0.0,
            "sum_net2": 0.0,
            "tp1_wins": 0,
            "sl_first": 0,
            "open_end": 0,
            "no_targets": 0,
            "sum_r": 0.0,
            "n_r": 0,
        }
        for half in ("val", "test")
    }


def fold_trade(acc: dict, half: str, res: dict) -> None:
    cell = acc[half]
    cell["n"] += 1
    net = res["net_pnl_pct"] / 100.0
    cell["sum_net"] += net
    cell["sum_net2"] += net * net
    if res["outcome_tp1"] == 1:
        cell["tp1_wins"] += 1
    elif res["outcome_tp1"] == 0:
        cell["sl_first"] += 1
    else:
        cell["open_end"] += 1
    if res.get("r_multiple") is not None:
        cell["sum_r"] += res["r_multiple"]
        cell["n_r"] += 1


def cell_stats(cell: dict) -> dict:
    n = cell["n"]
    if n == 0:
        return {
            "n": 0,
            "avg_net_pct": None,
            "tstat": None,
            "tp1_wr": None,
            "avg_r": None,
            "open_end": 0,
            "no_targets_skipped": cell["no_targets"],
        }
    mean = cell["sum_net"] / n
    var = max(cell["sum_net2"] / n - mean * mean, 0.0)
    t = mean / math.sqrt(var / n) if var > 0 and n > 1 else None
    closed = cell["tp1_wins"] + cell["sl_first"]
    return {
        "n": n,
        "avg_net_pct": round(100.0 * mean, 4),
        "tstat": round(t, 2) if t is not None else None,
        "tp1_wr": round(cell["tp1_wins"] / closed, 4) if closed else None,
        "open_end": cell["open_end"],
        "no_targets_skipped": cell["no_targets"],
        "avg_r": round(cell["sum_r"] / cell["n_r"], 3) if cell["n_r"] else None,
    }


def derive_verdict(acc: dict) -> dict:
    halves = {h: cell_stats(acc[h]) for h in ("val", "test")}
    ok = all(
        halves[h]["n"] >= MIN_HALF_N and halves[h]["avg_net_pct"] is not None and halves[h]["avg_net_pct"] > 0
        for h in ("val", "test")
    )
    return {"verdict": "PASS" if ok else "NO-DEPLOY", "min_half_n": MIN_HALF_N, "halves": halves}


# ── Per-symbol replay ────────────────────────────────────────────────────────


def detect_short_events(hm: pd.DataFrame, high: np.ndarray, close: np.ndarray) -> np.ndarray:
    """Indices of upper-band touch bars (gate-study semantics, SHORT side).

    Bar t qualifies iff the PRIOR bar's band exists, sits ≥ MIN_EVENT_BAND_DIST
    above the prior close, and high[t] reaches it. Warm-up excluded. Overlap
    (position-open) dedup happens in the replay loop, not here.
    """
    ub = hm["upper_band"].to_numpy()
    n = len(ub)
    if n == 0:
        return np.array([], dtype=np.int64)
    prev_band = np.concatenate(([np.nan], ub[:-1]))
    prev_close = np.concatenate(([np.nan], close[:-1]))
    with np.errstate(invalid="ignore", divide="ignore"):
        qualifies = (
            np.isfinite(prev_band)
            & np.isfinite(prev_close)
            & (high >= prev_band)
            & (prev_band / prev_close - 1.0 >= MIN_EVENT_BAND_DIST)
        )
    qualifies[:WARMUP_BARS] = False
    qualifies[n - 1 :] = False  # need at least one bar after entry for the exit scan
    return np.nonzero(qualifies)[0]


def replay_symbol(conn, symbol: str, start: dt.datetime, split_epoch: float, cfg: HeatmapConfig, acc: dict) -> int:
    candles = read_candles(conn, symbol, TF_EVENT, start=start, columns=("open_time", "open", "high", "low", "close"))
    if len(candles) < MIN_BARS:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ts, oi_value_usdt FROM oi_5m WHERE symbol = %s AND ts >= %s ORDER BY ts",
            (symbol, start),
        )
        oi = pd.DataFrame(cur.fetchall(), columns=["ts", "oi_value_usdt"])
    if len(oi) < MIN_BARS:
        return 0
    sr = read_candles(
        conn, symbol, TF_SR, start=start - dt.timedelta(days=31), columns=("open_time", "high", "low", "close")
    )
    if len(sr) < MIN_SR_BARS:
        return 0

    hm = build_heatmap(candles, oi, cfg)
    high = candles["high"].to_numpy()
    low = candles["low"].to_numpy()
    close = candles["close"].to_numpy()
    n = len(candles)
    open_ep = epoch_seconds(candles["open_time"])  # resolution-safe (T-073 lesson)
    close_ep = open_ep + float(BAR_SECONDS)
    # tz-naive UTC instants for simulate_exit (K11 convention)
    t_naive = candles["open_time"].dt.tz_convert("UTC").dt.tz_localize(None).to_numpy()

    sr_h = sr["high"].to_numpy()
    sr_l = sr["low"].to_numpy()
    sr_c = sr["close"].to_numpy()
    sr_close_ep = epoch_seconds(sr["open_time"]) + 900.0

    events = detect_short_events(hm, high, close)
    n_folded = 0
    last_exit_ep: float | None = None
    for i in events:
        i = int(i)
        ep = float(close_ep[i])
        if last_exit_ep is not None and ep < last_exit_ep:
            continue  # position still open (K11 re-entry convention)
        entry = float(close[i])
        if not (np.isfinite(entry) and entry > 0):
            continue
        # As-of S/R frame: trailing 15m bars whose CLOSE is at or before entry.
        sr_hi = int(np.searchsorted(sr_close_ep, ep, side="right"))
        if sr_hi < MIN_SR_BARS:
            continue
        sr_lo = max(0, sr_hi - SR_WINDOW_BARS)
        frame = pd.DataFrame({"high": sr_h[sr_lo:sr_hi], "low": sr_l[sr_lo:sr_hi], "close": sr_c[sr_lo:sr_hi]})
        supps, resis = get_hvn_and_sr_levels(None, None, entry, df=frame)
        del frame
        _e2, sl, t_cands = hvn_sr_trade_geometry(entry, False, supps, resis)
        targets = ensure_min_tp_distance(list(t_cands[:20]), entry, False, min_pct=MIN_TP_PCT)
        half = "val" if ep < split_epoch else "test"
        if not targets:
            acc[half]["no_targets"] += 1
            continue
        start_i = i + 1
        hi = min(n, start_i + EXIT_SCAN_CAP_BARS)
        if start_i >= hi:
            continue
        res = simulate_exit(
            t_naive[start_i:hi],
            high[start_i:hi],
            low[start_i:hi],
            close[start_i:hi],
            0,
            "SHORT",
            entry,
            sl,
            targets,
            min(N_PUBLISHED, len(targets)),
        )
        fold_trade(acc, half, res)
        n_folded += 1
        if res.get("exit_time"):
            last_exit_ep = pd.Timestamp(res["exit_time"]).tz_localize("UTC").timestamp()
        else:
            last_exit_ep = float(close_ep[hi - 1])  # open at scan end → blocked until cap end
    return n_folded


# ── Report ───────────────────────────────────────────────────────────────────


def write_outputs(meta: dict, acc: dict, verdict: dict) -> None:
    os.makedirs("staging_models", exist_ok=True)
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "verdict": verdict, "acc": acc}, f, indent=2, default=str)
    h = verdict["halves"]

    def row(name: str) -> str:
        c = h[name]
        avg_net = "—" if c["avg_net_pct"] is None else f"{c['avg_net_pct']}%"
        tstat = "—" if c["tstat"] is None else str(c["tstat"])
        wr = "—" if c["tp1_wr"] is None else f"{100.0 * c['tp1_wr']:.1f}%"
        avg_r = "—" if c["avg_r"] is None else str(c["avg_r"])
        return (
            f"| {name} | {c['n']} | {avg_net} | {tstat} | {wr} | {avg_r} | "
            f"{c['open_end']} | {c['no_targets_skipped']} |"
        )

    md = [
        "# MPS2 — Upper-Band SHORT Backtest, House Geometry (T-2026-KYT-9050-078)",
        "",
        f"Generated: {meta['generated_at']} · status: {meta['status']}",
        f"Universe: {meta['n_coins_done']}/{meta['n_coins']} symbols · trades: {meta['n_trades']:,}",
        f"Window: {meta['data_start']} → {meta['data_end']} (split {meta['split_iso']})",
        "",
        f"## VERDICT: **{verdict['verdict']}**",
        "",
        f"Gate (pre-registered): val AND test avg net PnL > 0 at n ≥ {MIN_HALF_N} per half.",
        "",
        "| half | n | avg net | t | TP1-WR (closed) | avg R | open@cap | skipped (no targets) |",
        "|---|---|---|---|---|---|---|---|",
        row("val"),
        row("test"),
        "",
        "## Wiring",
        "",
        "* Events: gate-study semantics (prior-bar upper band, ≥ 0.3 % above prior close, touch by high), "
        "no re-entry while the prior geometry exit is open.",
        "* Geometry: `get_hvn_and_sr_levels` (as-of trailing 30d 15m frame) → `hvn_sr_trade_geometry`(SHORT) → "
        f"`ensure_min_tp_distance`({MIN_TP_PCT:.0%}) → `simulate_exit` first-touch ladder, {N_PUBLISHED} TPs, "
        f"taker fee {FEE_PER_SIDE:.2%}/side, SL-first on same-bar ambiguity, scan cap {EXIT_SCAN_CAP_BARS} bars (4d).",
        "",
        "## Honest limits",
        "",
        "* SAME window as the MPS1 gate study → in-sample deployability check, NOT fresh evidence "
        "(T-007 lesson). Real out-of-sample = future oi_5m weeks.",
        "* Heatmap leverage mix is an assumption (see engine header); survivorship via coins.json.",
        "* The 5 % min-TP house rule sits far above the measured 4h drift (~0.16 %) — a NO-DEPLOY here "
        "falsifies the COMBINATION drift × house geometry, not the drift itself.",
        "",
    ]
    with open(MD_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(md))


def main() -> int:
    ap = argparse.ArgumentParser(description="MPS2 upper-band SHORT backtest (read-only)")
    ap.add_argument("--limit-symbols", type=int, default=0)
    ap.add_argument("--skip-cpu-check", action="store_true")
    ap.add_argument("--progress-every", type=int, default=20)
    ap.add_argument("--checkpoint-every", type=int, default=50)
    args = ap.parse_args()

    set_low_priority()
    if not args.skip_cpu_check:
        check_cpu_headroom()

    cfg = HeatmapConfig()
    acc = new_acc()
    n_trades = 0
    n_done = 0

    with db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT min(ts), max(ts) FROM oi_5m")
            oi_min, oi_max = cur.fetchone()
        if oi_min is None:
            print("oi_5m is empty - nothing to backtest")
            return 1
        split_dt = oi_min + (oi_max - oi_min) / 2
        split_epoch = split_dt.timestamp()

        with conn.cursor() as cur:
            cur.execute("SELECT DISTINCT symbol FROM oi_5m")
            oi_symbols = {r[0] for r in cur.fetchall()}
        coins = [c for c in load_coins(usdt_only=True, uppercase=True) if c in oi_symbols]
        if args.limit_symbols:
            coins = coins[: args.limit_symbols]
        print(f"universe: {len(coins)} symbols, window {oi_min} -> {oi_max}, split {split_dt}")

        def snapshot(final: bool) -> dict:
            verdict = derive_verdict(acc)
            meta = {
                "study": "MPS2 upper-band SHORT backtest (house geometry)",
                "task": "T-2026-KYT-9050-078",
                "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "status": "complete"
                if final and not args.limit_symbols
                else ("partial (sampling cap)" if final else "partial (checkpoint)"),
                "n_coins": len(coins),
                "n_coins_done": n_done,
                "n_trades": n_trades,
                "data_start": str(oi_min),
                "data_end": str(oi_max),
                "split_iso": str(split_dt),
                "fee_per_side": FEE_PER_SIDE,
                "config": {
                    "tf_event": TF_EVENT,
                    "tf_sr": TF_SR,
                    "sr_window_bars": SR_WINDOW_BARS,
                    "n_published": N_PUBLISHED,
                    "min_tp_pct": MIN_TP_PCT,
                    "exit_scan_cap_bars": EXIT_SCAN_CAP_BARS,
                    "min_event_band_dist": MIN_EVENT_BAND_DIST,
                    "warmup_bars": WARMUP_BARS,
                    "min_half_n": MIN_HALF_N,
                },
            }
            write_outputs(meta, acc, verdict)
            return verdict

        for sym in coins:
            try:
                n_trades += replay_symbol(conn, sym, oi_min, split_epoch, cfg, acc)
            except Exception as e:  # one bad coin must not kill the run
                conn.rollback()
                print(f"  WARN {sym}: {e}")
            n_done += 1
            if n_done % max(1, args.progress_every) == 0:
                print(f"  ...{n_done}/{len(coins)} symbols, {n_trades:,} trades")
            if n_done % max(1, args.checkpoint_every) == 0:
                snapshot(final=False)

        verdict = snapshot(final=True)

    print(f"\nVERDICT: {verdict['verdict']}")
    print(f"trades={n_trades:,} val={verdict['halves']['val']} test={verdict['halves']['test']}")
    print(f"wrote {JSON_PATH}")
    print(f"wrote {MD_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
