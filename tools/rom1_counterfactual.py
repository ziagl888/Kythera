"""
tools/rom1_counterfactual.py — ROM1 counterfactual scorer (T-2026-CU-9050-047).

Purpose
-------
The orchestrator gate (bot 28) suppresses signals without anyone ever having
measured what that suppression is worth (Report 16, §8). This tool computes
the hypothetical outcome for every row in `orchestrator_suppressed_signals`:
which ROM1 geometry would the orchestrator have posted at the signal moment,
and how would that trade have played out in the first-touch replay?

Result per suppression reason (`bot_not_whitelisted:wr_below_overall`,
`orchestrator_cooldown`, `bot_unidentified`, …): win rate, net PnL, R —
in other words **what the gate cost or saved**. Positive net PnL on the
suppressed side = the gate left money on the table.

Both sides of the same gate
----------------------------
`--side suppressed` (default) scores the blocked side.
`--side forwarded` scores the forwarded side from
`orchestrator_open_trades`, bucketed by `wl_reason` (B8,
T-2026-CU-9050-046) — i.e. per gate PATH: real 4D cell vs.
`no_whitelist_entry` (default-open) vs. fallback paths.
`--side both` runs both and lines the buckets up side by side.

Only the comparison of both sides answers the real question: does the
gate path separate winners from losers, or is the +8pp ROM1 WR an artefact of
the 89% default-open rate?

Methodology (and its limits)
---------------------------
  * **No look-ahead.** Decision candle = the last 1h candle that was already
    CLOSED at the time of the suppression. The exit scan starts
    on the candle after that (R1 discipline).
  * **Geometry from ONE source**: `28_signal_orchestrator.compute_rom1_trade_params`
    with the as-of parameters `price=`/`df=`. No reimplementation, no skew (X-R1).
  * **Exits** via `tools.walkforward_sim.simulate_exit` — wick-aware
    first-touch, SL-first on ambiguity, monitor trailing, fees; ladder over
    the 3 actually published TPs (`ROM1_PUBLISHED_TARGETS`).
  * **Deliberate approximations** (read in the report as the bias direction):
      - Live, ROM1 takes the last 5m close as CMP, the replay takes the close
        of the decision 1h candle (up to 59 minutes earlier).
      - The horizon is capped (`--horizon-hours`, default 168h). Live, a
        regime change would close the trade earlier (auto-close) — the
        counterfactuals are therefore rather optimistic for long runners.
      - `same_direction_open`/`opposite_direction_open`/`orchestrator_cooldown`
        are **dedupe**, not a regime verdict: their counterfactual measures the
        value of position hygiene, not the quality of the 4D gate. The output
        therefore separates the classes (`bucket_class`).

Operating rules (live VPS!)
--------------------------
  * DB strictly read-only (SELECTs only), process at BELOW_NORMAL, CPU check —
    identical to walkforward_sim. No table is written.
  * Results as JSONL + summary JSON under `KYTHERA_REPLAY_DIR`.

Examples
---------
  python tools/rom1_counterfactual.py --days 90
  python tools/rom1_counterfactual.py --days 90 --side both --horizon-hours 72
  python tools/rom1_counterfactual.py --days 30 --side forwarded --limit 200
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from datetime import timedelta

import numpy as np
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from core.candles import read_candles  # noqa: E402
from core.database import get_db_connection  # noqa: E402
from core.time import utc_now  # noqa: E402
from tools.walkforward_sim import (  # noqa: E402
    check_cpu_headroom,
    import_bot_module,
    set_low_priority,
    simulate_exit,
)

DEFAULT_OUT_DIR = os.getenv(
    "KYTHERA_REPLAY_DIR", r"C:\Users\Michael\Documents\_X\staging_models\replay"
)

# get_hvn_and_sr_levels reads 95 days of 1h candles live and needs >= 50 rows.
SR_WINDOW_HOURS = 95 * 24
MIN_SR_ROWS = 50
DEFAULT_HORIZON_HOURS = 168

OHLCV_COLUMNS = ("open_time", "open", "high", "low", "close", "volume")

# What each reason stands for — only `gate` measures the 4D whitelist verdict.
REASON_CLASS = {
    "bot_not_whitelisted": "gate",
    "orchestrator_cooldown": "dedupe",
    "opposite_direction_open": "dedupe",
    "same_direction_open": "dedupe",
    "bot_unidentified": "plumbing",
    "rom1_params_unavailable": "plumbing",
}


# ─────────────────────────────────────────────────────────────────────────────
# REASON BUCKETS
# ─────────────────────────────────────────────────────────────────────────────
def parse_reason(reason: str | None) -> tuple[str, str]:
    """`reason` → (bucket, bucket_class).

    The whitelist block carries the actual gate path in the suffix
    (`bot_not_whitelisted:wr_below_overall`). This suffix is exactly the
    interesting axis — the prefix alone would be a single 90% bucket.
    """
    if not reason:
        return "unknown", "unknown"
    family, _, detail = reason.partition(":")
    bucket = f"{family}:{detail}" if detail else family
    return bucket, REASON_CLASS.get(family, "unknown")


def forwarded_bucket(wl_reason: str | None) -> tuple[str, str]:
    """Forwarded side: the gate path lives in `orchestrator_open_trades.wl_reason`.

    Rows from before B8 (T-2026-CU-9050-046) have NULL — those are counted as
    their own bucket instead of being attributed to a path.
    """
    if not wl_reason:
        return "forwarded:wl_reason_missing", "forward"
    return f"forwarded:{wl_reason}", "forward"


# ─────────────────────────────────────────────────────────────────────────────
# DB (read-only)
# ─────────────────────────────────────────────────────────────────────────────
def load_suppressed(conn, days: int, limit: int | None) -> list[dict]:
    """Suppressed rows from the last `days` days.

    `ts` is naive UTC (default `NOW() AT TIME ZONE 'UTC'`, 26_regime_detector) —
    comparing against NOW() would be session-local (UTC_POLICY §R3). We
    therefore cut against an explicitly computed naive UTC cutoff.
    """
    cutoff = utc_now().replace(tzinfo=None) - timedelta(days=int(days))
    sql = """
        SELECT id, ts, bot_name, coin, direction, regime_at_signal, reason, original_outbox_id
        FROM orchestrator_suppressed_signals
        WHERE ts >= %s AND coin IS NOT NULL AND direction IS NOT NULL
        ORDER BY ts ASC
    """
    params: list = [cutoff]
    if limit:
        sql += " LIMIT %s"
        params.append(int(limit))
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    out = []
    for r in rows:
        bucket, cls = parse_reason(r[6])
        out.append({
            "side": "suppressed", "row_id": r[0], "ts": r[1], "bot_name": r[2],
            "coin": r[3], "direction": r[4], "regime_at_signal": r[5],
            "reason": r[6], "bucket": bucket, "bucket_class": cls,
            "original_outbox_id": r[7], "recorded_entry": None,
        })
    return out


def load_forwarded(conn, days: int, limit: int | None) -> list[dict]:
    """Forwarded rows (the passed-through side), bucketed by wl_reason."""
    cutoff = utc_now().replace(tzinfo=None) - timedelta(days=int(days))
    sql = """
        SELECT id, opened_at, bot_name, coin, direction, regime_at_open,
               wl_reason, original_outbox_id, entry_price
        FROM orchestrator_open_trades
        WHERE opened_at >= %s
        ORDER BY opened_at ASC
    """
    params: list = [cutoff]
    if limit:
        sql += " LIMIT %s"
        params.append(int(limit))
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
    out = []
    for r in rows:
        bucket, cls = forwarded_bucket(r[6])
        out.append({
            "side": "forwarded", "row_id": r[0], "ts": r[1], "bot_name": r[2],
            "coin": r[3], "direction": r[4], "regime_at_signal": r[5],
            "reason": r[6], "bucket": bucket, "bucket_class": cls,
            "original_outbox_id": r[7],
            "recorded_entry": float(r[8]) if r[8] is not None else None,
        })
    return out


def load_1h(conn, coin: str, oldest_ts, horizon_hours: int) -> pd.DataFrame | None:
    """1h candles from (oldest signal time − S/R window), CLOSED only.

    The window deliberately reaches far back before the first signal: the
    S/R level computation needs the same 95 days of history that the live bot
    would have seen at posting time.
    """
    start = pd.Timestamp(oldest_ts)
    if start.tzinfo is None:
        start = start.tz_localize("UTC")
    start = (start - pd.Timedelta(hours=SR_WINDOW_HOURS + 2)).to_pydatetime()
    try:
        df = read_candles(conn, coin, "1h", start=start, include_forming=False, columns=OHLCV_COLUMNS)
    except Exception:
        conn.rollback()
        return None
    if df.empty:
        return None
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["high", "low", "close"]).reset_index(drop=True)
    return df if len(df) >= MIN_SR_ROWS else None


# ─────────────────────────────────────────────────────────────────────────────
# AS-OF INDEX (R1: closed candles only)
# ─────────────────────────────────────────────────────────────────────────────
def as_of_index(open_times: np.ndarray, ts) -> int:
    """Index of the last 1h candle that was already CLOSED at time `ts`.

    A candle with open_time `o` closes at `o + 1h`. So we are looking for the
    last `o` with `o + 1h <= ts`, i.e. `o <= ts - 1h`. The candle that contains
    `ts` is still forming at decision time and must not be seen —
    this is exactly where look-ahead otherwise sneaks in (R1 trap).

    `open_times` is the naive-UTC datetime64 array from `df["open_time"].values`.
    Returns -1 if no candle lies early enough.
    """
    t = pd.Timestamp(ts)
    if t.tzinfo is not None:
        t = t.tz_convert("UTC").tz_localize(None)
    cutoff = (t - pd.Timedelta(hours=1)).to_datetime64()
    return int(np.searchsorted(open_times, cutoff, side="right")) - 1


# ─────────────────────────────────────────────────────────────────────────────
# SCORING
# ─────────────────────────────────────────────────────────────────────────────
def score_row(orch, row: dict, df: pd.DataFrame, horizon_hours: int) -> dict:
    """Score one signal counterfactually. Returns the enriched record.

    `scored=False` + `skip_reason` for rows that cannot be scored (too short
    a history, no geometry) — these are counted, not silently
    dropped: an evaluation that lets 30% of suppressions fall through the
    cracks no longer measures the gate value.
    """
    rec = dict(row)
    rec["ts"] = str(row["ts"])
    direction = (row["direction"] or "").upper()
    if direction not in ("LONG", "SHORT"):
        return {**rec, "scored": False, "skip_reason": "bad_direction"}

    open_times = df["open_time"].values
    t = as_of_index(open_times, row["ts"])
    if t < MIN_SR_ROWS:
        return {**rec, "scored": False, "skip_reason": "insufficient_history"}
    if t >= len(df) - 1:
        return {**rec, "scored": False, "skip_reason": "no_forward_candles"}

    entry_price = float(df["close"].values[t])
    if not np.isfinite(entry_price) or entry_price <= 0:
        return {**rec, "scored": False, "skip_reason": "bad_entry_price"}

    win = df.iloc[max(0, t + 1 - SR_WINDOW_HOURS): t + 1][["high", "low", "close"]]
    params = orch.compute_rom1_trade_params(None, row["coin"], direction, price=entry_price, df=win)
    if params is None:
        return {**rec, "scored": False, "skip_reason": "rom1_params_unavailable"}

    # Horizon capping: the arrays end at the horizon, `open_at_end` then means
    # "after N hours neither TP1 nor SL" (remainder mark-to-market at horizon close).
    end = min(len(df), t + 1 + int(horizon_hours))
    res = simulate_exit(
        open_times[:end],
        df["high"].values[:end],
        df["low"].values[:end],
        df["close"].values[:end],
        t + 1,
        direction,
        params["entry1"],
        params["sl"],
        params["targets"],
        orch.ROM1_PUBLISHED_TARGETS,
    )
    rec.update({
        "scored": True,
        "skip_reason": None,
        "decision_candle": str(pd.Timestamp(open_times[t])),
        "entry": params["entry1"],
        "sl": params["sl"],
        "targets": params["targets"][: orch.ROM1_PUBLISHED_TARGETS],
        "horizon_hours": int(horizon_hours),
        "full_horizon": end == t + 1 + int(horizon_hours),
        **res,
    })
    if row.get("recorded_entry"):
        # Drift between live CMP (5m close) and replay entry (1h close) —
        # the yardstick for the "up to 59 minutes earlier" approximation.
        rec["entry_drift_pct"] = round(
            (params["entry1"] - row["recorded_entry"]) / row["recorded_entry"] * 100, 4
        )
    return rec


# ─────────────────────────────────────────────────────────────────────────────
# AGGREGATION
# ─────────────────────────────────────────────────────────────────────────────
def aggregate(records: list[dict]) -> list[dict]:
    """Per bucket: n, win rate, PnL, R. Sorted by signal count.

    `n_open_at_horizon` are trades that had touched neither TP1 nor SL at the
    horizon — they do NOT count towards the win rate (no label), but their
    mark-to-market PnL does flow into the PnL sum, because the position would
    really have been open.
    """
    by_bucket: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_bucket[r["bucket"]].append(r)

    out = []
    for bucket, rows in by_bucket.items():
        scored = [r for r in rows if r.get("scored")]
        decided = [r for r in scored if r.get("outcome_tp1") is not None]
        wins = sum(1 for r in decided if r["outcome_tp1"] == 1)
        pnl = [r["net_pnl_pct"] for r in scored if r.get("net_pnl_pct") is not None]
        r_vals = [r["r_multiple"] for r in scored if r.get("r_multiple") is not None]
        skips: dict[str, int] = defaultdict(int)
        for r in rows:
            if not r.get("scored"):
                skips[r.get("skip_reason") or "unknown"] += 1
        out.append({
            "bucket": bucket,
            "bucket_class": rows[0]["bucket_class"],
            "side": rows[0]["side"],
            "n_signals": len(rows),
            "n_scored": len(scored),
            "n_unscorable": len(rows) - len(scored),
            "unscorable_by_reason": dict(skips),
            "n_decided": len(decided),
            "n_open_at_horizon": len(scored) - len(decided),
            "tp1_first_touch_wr": round(wins / len(decided) * 100, 2) if decided else None,
            "sum_net_pnl_pct": round(float(np.sum(pnl)), 2) if pnl else None,
            "avg_net_pnl_pct": round(float(np.mean(pnl)), 4) if pnl else None,
            "median_net_pnl_pct": round(float(np.median(pnl)), 4) if pnl else None,
            "avg_r": round(float(np.mean(r_vals)), 4) if r_vals else None,
        })
    return sorted(out, key=lambda d: -d["n_signals"])


def print_report(summary: list[dict]) -> None:
    if not summary:
        print("No rows in the window — nothing to score.")
        return
    hdr = f"{'bucket':46} {'class':9} {'n':>6} {'scored':>7} {'wr%':>7} {'avgPnL%':>9} {'sumPnL%':>10} {'avgR':>7}"
    print("\n" + hdr)
    print("-" * len(hdr))
    for s in summary:
        wr = f"{s['tp1_first_touch_wr']:.2f}" if s["tp1_first_touch_wr"] is not None else "—"
        avg = f"{s['avg_net_pnl_pct']:.3f}" if s["avg_net_pnl_pct"] is not None else "—"
        tot = f"{s['sum_net_pnl_pct']:.2f}" if s["sum_net_pnl_pct"] is not None else "—"
        avr = f"{s['avg_r']:.3f}" if s["avg_r"] is not None else "—"
        print(f"{s['bucket'][:46]:46} {s['bucket_class']:9} {s['n_signals']:6d} "
              f"{s['n_scored']:7d} {wr:>7} {avg:>9} {tot:>10} {avr:>7}")

    gate = [s for s in summary if s["bucket_class"] == "gate" and s["avg_net_pnl_pct"] is not None]
    fwd = [s for s in summary if s["bucket_class"] == "forward" and s["avg_net_pnl_pct"] is not None]
    if gate:
        n = sum(s["n_scored"] for s in gate)
        tot = sum(s["sum_net_pnl_pct"] for s in gate)
        print(f"\nGate side (bot_not_whitelisted, {n} scored suppressions): "
              f"total {tot:.2f}% notional — positive = the gate left money on the table.")
    if fwd:
        n = sum(s["n_scored"] for s in fwd)
        tot = sum(s["sum_net_pnl_pct"] for s in fwd)
        print(f"Forward side ({n} scored forwards): total {tot:.2f}% notional.")
    print("\nReading note: `dedupe` buckets measure position hygiene, not the 4D gate. "
          "Only `gate` vs `forward` at the same horizon are comparable.")


# ─────────────────────────────────────────────────────────────────────────────
# DRIVER
# ─────────────────────────────────────────────────────────────────────────────
def score_all(conn, orch, rows: list[dict], horizon_hours: int) -> list[dict]:
    """Work through rows coin by coin — one candle load per coin, not per signal."""
    by_coin: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_coin[r["coin"]].append(r)

    records: list[dict] = []
    t0 = time.time()
    for i, (coin, coin_rows) in enumerate(sorted(by_coin.items()), 1):
        df = load_1h(conn, coin, min(r["ts"] for r in coin_rows), horizon_hours)
        if df is None:
            records.extend({**r, "ts": str(r["ts"]), "scored": False, "skip_reason": "no_candles"} for r in coin_rows)
            continue
        for r in coin_rows:
            try:
                records.append(score_row(orch, r, df, horizon_hours))
            except Exception as e:  # one broken row does not abort the run
                print(f"  !! {coin} row#{r['row_id']}: {e}")
                records.append({**r, "ts": str(r["ts"]), "scored": False, "skip_reason": "error"})
        if i % 25 == 0 or i == len(by_coin):
            print(f"[{i}/{len(by_coin)}] {coin}: {len(records)} rows ({time.time() - t0:.0f}s)", flush=True)
    return records


def main() -> None:
    ap = argparse.ArgumentParser(description="ROM1 Counterfactual Scorer (T-2026-CU-9050-047)")
    ap.add_argument("--days", type=int, default=90, help="lookback over ts/opened_at")
    ap.add_argument("--side", default="suppressed", choices=["suppressed", "forwarded", "both"])
    ap.add_argument("--horizon-hours", type=int, default=DEFAULT_HORIZON_HOURS)
    ap.add_argument("--limit", type=int, default=None, help="only the first N rows per side")
    ap.add_argument("--out", default=DEFAULT_OUT_DIR)
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    set_low_priority()
    check_cpu_headroom()

    orch = import_bot_module("28_signal_orchestrator.py", "signal_orchestrator")

    conn = get_db_connection()
    try:
        conn.set_session(readonly=True)  # the scorer NEVER writes to the live DB
    except Exception:
        pass

    try:
        rows: list[dict] = []
        if args.side in ("suppressed", "both"):
            rows += load_suppressed(conn, args.days, args.limit)
        if args.side in ("forwarded", "both"):
            rows += load_forwarded(conn, args.days, args.limit)
        print(f"{len(rows)} rows in the window ({args.days}d, side={args.side})")
        records = score_all(conn, orch, rows, args.horizon_hours)
    finally:
        conn.close()

    summary = aggregate(records)
    os.makedirs(args.out, exist_ok=True)
    tag = f"rom1_counterfactual_{args.side}_{args.days}d"
    jsonl_path = os.path.join(args.out, f"{tag}.jsonl")
    with open(jsonl_path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, default=str) + "\n")
    meta = {
        "days": args.days,
        "side": args.side,
        "horizon_hours": args.horizon_hours,
        "n_rows": len(records),
        "n_scored": sum(1 for r in records if r.get("scored")),
        "generated_at": str(utc_now()),
        "buckets": summary,
    }
    with open(os.path.join(args.out, f"{tag}_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, default=str)

    print_report(summary)
    print(f"\nRecords: {jsonl_path}")


if __name__ == "__main__":
    main()
