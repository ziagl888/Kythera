# tools/leg_composition_replay.py — per-leg edge against candles, not against the book.
"""Candle replay of every fleet signal, grouped by model x direction x regime cohort.

T-2026-KYT-9050-104. The question this answers is "which legs belong in a
Cornix-traded channel, in which direction, and how do we keep the book from
going one-sided into a dump" — and it answers it against historical candles
instead of against the bots' own closing book.

Two halves, on purpose
----------------------
``export`` touches the DB, ``replay`` does not. The replay is the expensive part
(3.9M candles against 44k signals) and the live VPS sits at a measured 100% CPU
for hours, so it has no business running there. ``export`` is a single sequential
read into a compressed ``.npz`` (~50-70 MB) that can be carried to any machine;
``replay`` needs numpy and nothing else — no ``.env``, no credentials, no
network. That also makes the analysis reproducible off-box and testable
DB-free, the same way ``backtest/test_*.py`` are.

Why not the book
----------------
``closed_ai_signals`` carries the monitor's verdict, not a realizable one. Two
independent gaps, both measured on 2026-08-05:

* The book credits the whole position at the final close price, while Cornix
  splits the size across the TP ladder. For AIM2 the book reports +0.69 pp per
  trade where the Cornix run implies +0.054 pp — a factor of 13.
* ``status`` embeds an SL price, but it is the **monitor-trailed** value, not the
  one that was published. Bucketing trades by it conditions the bucket on the
  outcome: the "<3% SL" bucket comes out at a 94.7% TP1 rate because those are
  the trades that ran far enough for the SL to be pulled up behind them.

So the original SL is not reconstructible for closed signals, and this tool does
not try. It replays every signal against a **standardised** (TP, SL) grid, which
is what a composition decision needs anyway: legs have to be comparable on one
geometry before they can be ranked against each other.

The regime split is not optional
--------------------------------
``EXPOSURE_CAP`` and the time-stop went live on **2026-07-28 14:00Z**. Across that
boundary the trailing book changes character completely — 83% LONG at
-1.342 pp/trade before, 51% LONG at +0.191 pp/trade after. Pooling the two
cohorts once already produced a confident and wrong verdict ("MIS1-72H is the
loss engine"; post-cutoff that leg runs +0.38 pp over 149 trades). The split is
therefore hard-wired, and every aggregate is reported per cohort.

Replay conventions
------------------
* **First touch, wick-aware.** A level counts as reached when a candle's high
  (LONG target / SHORT stop) or low crosses it.
* **Evaluation starts at the first candle strictly after the signal timestamp.**
  Intra-candle order is not determinable, so the entry candle cannot decide a
  TP — same convention as ``audit_reports/19_batch_e_trainer_fixes.md``.
* **TP and SL in the same candle book as SL.** Conservative on purpose: the
  alternative silently converts stop-outs into wins.
* **Unresolved at the horizon is marked to market**, at the last close inside the
  horizon, and reported separately. Dropping those rows would keep only the
  trades that resolved, which is survivorship by another name.

Times are carried as epoch seconds throughout (T-073: a ``datetime64[us]`` round
trip silently rescales and the resulting join is off by orders of magnitude).

Read-only against the live DB (hard rule 1); the export runs at BELOW_NORMAL
priority behind the shared CPU guard, and the measured load goes into the file
rather than being claimed as headroom.

Usage
-----
    # on the VPS (needs .env):
    python tools/leg_composition_replay.py export --out reports/legs_raw.npz --force-on-busy
    # anywhere (needs numpy only):
    python tools/leg_composition_replay.py replay --in reports/legs_raw.npz --out reports/legs.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np

UTC = timezone.utc

# EXPOSURE_CAP + time-stop go-live. See module docstring — deliberately not a CLI
# flag: a pooled run is exactly the failure mode this constant exists to prevent.
REGIME_CUTOFF_EPOCH = int(datetime(2026, 7, 28, 14, 0, tzinfo=UTC).timestamp())

# Grid extended downward on 2026-08-05: at the original floor of 3.0 nearly every
# SHORT leg optimised on the smallest available SL, i.e. the run was reading its
# own boundary rather than an optimum. A grid whose best cell sits on the edge
# has not found anything — it has run out of room.
TP_GRID = (1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0)
SL_GRID = (1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 8.0)
FEE_PCT = 0.09  # taker round trip on notional
CANDLE_TF = "5m"
MIN_N = 40  # below this a model x direction cell is reported but not ranked


# ─────────────────────────────────────────────────────────────────────────────
# EXPORT (the only half that touches the DB)
# ─────────────────────────────────────────────────────────────────────────────


def cmd_export(args: argparse.Namespace) -> None:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from core.database import get_db_connection
    from tools.walkforward_sim import set_low_priority
    from tools.whitelist_v2_realized_eval import wait_for_cpu_headroom

    set_low_priority()
    cpu = wait_for_cpu_headroom(args.cpu_wait_min, args.force_on_busy)

    since = datetime.fromisoformat(args.since).replace(tzinfo=UTC)
    horizon = timedelta(hours=args.horizon_h)
    # Signals younger than the horizon cannot complete — including them would
    # right-censor the newest cohort into looking "unresolved".
    until = datetime.now(UTC) - horizon

    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")

            # ai_signals.timestamp is timestamptz; closed_ai_signals.open_time is a
            # legacy naive column read as UTC (core.time ships R3_CUTOVER_UTC=None,
            # i.e. a single domain over the whole history).
            cur.execute(
                """
                SELECT symbol, model, direction,
                       extract(epoch FROM (open_time AT TIME ZONE 'UTC'))::bigint, entry::float8
                  FROM closed_ai_signals
                 WHERE open_time >= %(s)s AND open_time < %(u)s
                   AND entry > 0 AND direction IN ('LONG','SHORT')
                UNION ALL
                SELECT symbol, model, direction,
                       extract(epoch FROM timestamp)::bigint, entry1::float8
                  FROM ai_signals
                 WHERE timestamp >= %(stz)s AND timestamp < %(utz)s
                   AND entry1 > 0 AND direction IN ('LONG','SHORT')
                """,
                {"s": since.replace(tzinfo=None), "u": until.replace(tzinfo=None), "stz": since, "utz": until},
            )
            sig_rows = cur.fetchall()
            if not sig_rows:
                raise SystemExit("No signals in the window — nothing to export.")

            symbols = sorted({r[0] for r in sig_rows})
            sym_index = {s: i for i, s in enumerate(symbols)}
            print(f"Signals: {len(sig_rows):,} over {len(symbols)} symbols. Fetching {CANDLE_TF} candles ...")

            # Per symbol rather than one 3.9M-row fetchall: psycopg2 materialises
            # every row as a Python tuple (~1 GB here), and this box is shared
            # with the fleet. 531 indexed range scans stay flat in memory and
            # give progress; the alternative is one query that can OOM the run
            # after twenty minutes of work.
            chunks: list[tuple[int, np.ndarray, np.ndarray, np.ndarray, np.ndarray]] = []
            n_candles = 0
            for i, symbol in enumerate(symbols, 1):
                cur.execute(
                    """SELECT extract(epoch FROM open_time)::bigint, high, low, close
                         FROM candles
                        WHERE tf = %s AND symbol = %s AND open_time >= %s AND open_time <= %s
                        ORDER BY open_time""",
                    (CANDLE_TF, symbol, since, until + horizon),
                )
                block = cur.fetchall()
                if not block:
                    continue
                n_candles += len(block)
                chunks.append(
                    (
                        sym_index[symbol],
                        np.fromiter((r[0] for r in block), dtype=np.int64, count=len(block)),
                        np.fromiter((r[1] for r in block), dtype=np.float64, count=len(block)),
                        np.fromiter((r[2] for r in block), dtype=np.float64, count=len(block)),
                        np.fromiter((r[3] for r in block), dtype=np.float64, count=len(block)),
                    )
                )
                if i % 50 == 0 or i == len(symbols):
                    print(f"  {i}/{len(symbols)} symbols, {n_candles:,} candles", flush=True)
    finally:
        conn.close()

    # concatenate once — the replay relies on candles being ordered by (symbol, ts)
    c_sym = np.concatenate([np.full(len(c[1]), c[0], dtype=np.int32) for c in chunks])
    c_ts = np.concatenate([c[1] for c in chunks])
    c_high = np.concatenate([c[2] for c in chunks])
    c_low = np.concatenate([c[3] for c in chunks])
    c_close = np.concatenate([c[4] for c in chunks])
    del chunks
    models = sorted({r[1] for r in sig_rows})
    mod_index = {m: i for i, m in enumerate(models)}

    payload = {
        "symbols": np.array(symbols, dtype=object),
        "models": np.array(models, dtype=object),
        "c_sym": c_sym,
        "c_ts": c_ts,
        "c_high": c_high,
        "c_low": c_low,
        "c_close": c_close,
        "s_sym": np.fromiter((sym_index[r[0]] for r in sig_rows), dtype=np.int32, count=len(sig_rows)),
        "s_mod": np.fromiter((mod_index[r[1]] for r in sig_rows), dtype=np.int32, count=len(sig_rows)),
        "s_long": np.fromiter((r[2] == "LONG" for r in sig_rows), dtype=bool, count=len(sig_rows)),
        "s_ts": np.fromiter((r[3] for r in sig_rows), dtype=np.int64, count=len(sig_rows)),
        "s_entry": np.fromiter((r[4] for r in sig_rows), dtype=np.float64, count=len(sig_rows)),
        "meta": np.array(
            [json.dumps({"since": args.since, "horizon_h": args.horizon_h, "tf": CANDLE_TF, "cpu_at_export": cpu})],
            dtype=object,
        ),
    }
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    np.savez_compressed(args.out, **payload)
    print(f"Written: {args.out}  ({os.path.getsize(args.out) / 1e6:.1f} MB) — carry this file, the replay needs no DB.")


# ─────────────────────────────────────────────────────────────────────────────
# REPLAY (numpy only — no DB, no .env, no network)
# ─────────────────────────────────────────────────────────────────────────────


def replay_signal(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, entry: float, is_long: bool
) -> tuple[dict, dict, float, float, float]:
    """First-touch step index per threshold, plus MFE/MAE and the horizon mark.

    Vectorised on purpose. The running maxima are monotone, so ``searchsorted``
    finds the first crossing in one C-level call per threshold instead of a
    Python loop over ~860 candles per signal (44k signals would make that the
    dominant cost, T-073).
    """
    if is_long:
        fav = (high - entry) / entry * 100.0
        adv = (entry - low) / entry * 100.0
    else:
        fav = (entry - low) / entry * 100.0
        adv = (high - entry) / entry * 100.0
    cum_fav = np.maximum.accumulate(fav)
    cum_adv = np.maximum.accumulate(adv)
    n = len(fav)

    def first_at(cum: np.ndarray, level: float) -> int | None:
        idx = int(np.searchsorted(cum, level, side="left"))
        return idx if idx < n else None

    i_tp = {str(d): first_at(cum_fav, d) for d in TP_GRID}
    i_sl = {str(s): first_at(cum_adv, s) for s in SL_GRID}
    mark = (close[-1] - entry) / entry * 100.0 * (1 if is_long else -1)
    return i_tp, i_sl, mark, float(cum_fav[-1]), float(cum_adv[-1])


def outcome(i_tp: int | None, i_sl: int | None) -> str:
    """TP only when strictly earlier — a tie inside one candle books as SL."""
    if i_tp is None and i_sl is None:
        return "OPEN"
    if i_tp is None:
        return "SL"
    if i_sl is None:
        return "TP"
    return "TP" if i_tp < i_sl else "SL"


def cmd_replay(args: argparse.Namespace) -> None:
    z = np.load(args.infile, allow_pickle=True)
    meta = json.loads(str(z["meta"][0]))
    horizon_s = int(meta["horizon_h"] * 3600)
    symbols, models = list(z["symbols"]), list(z["models"])
    c_sym, c_ts, c_high, c_low, c_close = z["c_sym"], z["c_ts"], z["c_high"], z["c_low"], z["c_close"]
    s_sym, s_mod, s_long, s_ts, s_entry = z["s_sym"], z["s_mod"], z["s_long"], z["s_ts"], z["s_entry"]
    print(f"Loaded {len(s_ts):,} signals / {len(c_ts):,} candles from {args.infile} (export meta: {meta})")

    # candles arrive ordered by (symbol, open_time) — one contiguous block per symbol
    starts = np.searchsorted(c_sym, np.arange(len(symbols)), side="left")
    ends = np.searchsorted(c_sym, np.arange(len(symbols)), side="right")

    rows: list[dict] = []
    skipped = 0
    for k in range(len(s_ts)):
        a, b = starts[s_sym[k]], ends[s_sym[k]]
        if a == b:
            skipped += 1
            continue
        ts = c_ts[a:b]
        # strictly after the signal: the entry candle cannot decide a TP
        lo = a + int(np.searchsorted(ts, s_ts[k], side="right"))
        hi = a + int(np.searchsorted(ts, s_ts[k] + horizon_s, side="right"))
        if hi <= lo:
            skipped += 1
            continue
        i_tp, i_sl, mark, mfe, mae = replay_signal(
            c_high[lo:hi], c_low[lo:hi], c_close[lo:hi], float(s_entry[k]), bool(s_long[k])
        )
        rows.append(
            {
                "model": models[s_mod[k]],
                "direction": "LONG" if s_long[k] else "SHORT",
                "cohort": "post" if s_ts[k] >= REGIME_CUTOFF_EPOCH else "pre",
                "ts": int(s_ts[k]),  # kept so stability can be measured per window
                "i_tp": i_tp,
                "i_sl": i_sl,
                "mark": mark,
                "mfe": mfe,
                "mae": mae,
            }
        )
    print(f"Replayed {len(rows):,} signals ({skipped:,} without candle coverage).")

    agg = aggregate(rows)
    payload = {
        "task": "T-2026-KYT-9050-104",
        "export_meta": meta,
        "regime_cutoff_epoch": REGIME_CUTOFF_EPOCH,
        "fee_pct": FEE_PCT,
        "tp_grid": list(TP_GRID),
        "sl_grid": list(SL_GRID),
        "n_replayed": len(rows),
        "n_skipped": skipped,
        "cells": agg,
    }
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1)
    print(f"Written: {args.out}  ({len(agg)} model x direction x cohort cells)")
    print_summary(agg, args.tp, args.sl)


def aggregate(rows: list[dict]) -> dict:
    """(model, direction, cohort) -> per (tp, sl) counts and expectancy."""
    cells: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    for r in rows:
        cells[(r["model"], r["direction"], r["cohort"])].append(r)

    out = {}
    for (model, direction, cohort), sub in cells.items():
        grid = {}
        for tp in TP_GRID:
            for sl in SL_GRID:
                n_tp = n_sl = n_open = 0
                total = 0.0
                for r in sub:
                    res = outcome(r["i_tp"][str(tp)], r["i_sl"][str(sl)])
                    if res == "TP":
                        n_tp += 1
                        total += tp
                    elif res == "SL":
                        n_sl += 1
                        total -= sl
                    else:
                        n_open += 1
                        total += r["mark"]
                grid[f"{tp}/{sl}"] = {
                    "tp": n_tp,
                    "sl": n_sl,
                    "open": n_open,
                    "exp_pp": round(total / len(sub) - FEE_PCT, 4),
                }
        mfe = sorted(r["mfe"] for r in sub)
        mae = sorted(r["mae"] for r in sub)
        out[f"{model}|{direction}|{cohort}"] = {
            "n": len(sub),
            "mfe_median": round(mfe[len(mfe) // 2], 3),
            "mae_median": round(mae[len(mae) // 2], 3),
            "grid": grid,
        }
    return out


def print_summary(agg: dict, tp: float, sl: float) -> None:
    key = f"{tp}/{sl}"
    print(f"\n=== Ranking at TP {tp}% / SL {sl}% (post-cutoff cohort, n >= {MIN_N}) ===")
    print(f"  {'Model':<14}{'Dir':<7}{'n':>6}{'TP':>7}{'SL':>7}{'open':>7}{'MFE med':>9}{'exp pp':>10}")
    ranked = [(k, v) for k, v in agg.items() if k.endswith("|post") and v["n"] >= MIN_N and key in v["grid"]]
    ranked.sort(key=lambda kv: -kv[1]["grid"][key]["exp_pp"])
    long_n = short_n = 0
    for k, v in ranked:
        model, direction, _ = k.split("|")
        g = v["grid"][key]
        print(
            f"  {model:<14}{direction:<7}{v['n']:>6}{g['tp']:>7}{g['sl']:>7}{g['open']:>7}"
            f"{v['mfe_median']:>9.2f}{g['exp_pp']:>+10.3f}"
        )
        if g["exp_pp"] > 0:
            if direction == "LONG":
                long_n += v["n"]
            else:
                short_n += v["n"]
    tot = long_n + short_n
    if tot:
        print(
            f"\n  Positive-expectancy legs alone would compose the book {long_n / tot:.0%} LONG / {short_n / tot:.0%} SHORT."
        )
        print("  Direction balance is a channel-level constraint (EXPOSURE_CAP), never an emergent")
        print("  property of an expectancy ranking — 27.07. was 81% LONG intake and cost -933 pp.")


def main() -> None:
    ap = argparse.ArgumentParser(description="Per-leg candle replay for channel composition (T-2026-KYT-9050-104)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    ex = sub.add_parser("export", help="read-only DB export to a portable .npz (VPS only)")
    ex.add_argument("--since", default="2026-07-11", help="ISO date, inclusive (default: 5m candle coverage)")
    ex.add_argument("--horizon-h", type=int, default=72)
    ex.add_argument("--out", default="reports/leg_composition_raw.npz")
    ex.add_argument("--cpu-wait-min", type=int, default=0)
    ex.add_argument("--force-on-busy", action="store_true")
    ex.set_defaults(func=cmd_export)

    rp = sub.add_parser("replay", help="replay the export — numpy only, no DB")
    rp.add_argument("--in", dest="infile", default="reports/leg_composition_raw.npz")
    rp.add_argument("--out", default="reports/leg_composition_replay.json")
    rp.add_argument("--tp", type=float, default=4.0, help="TP level for the printed ranking")
    rp.add_argument("--sl", type=float, default=5.0, help="SL level for the printed ranking")
    rp.set_defaults(func=cmd_replay)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
