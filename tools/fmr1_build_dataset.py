"""
tools/fmr1_build_dataset.py — training events + replay labels for FMR1
"Funding Extreme Mean-Reversion" (Report 15, p8). Runs on VPS (step 2).

Prerequisite: funding_rates is populated (python tools/backfill_funding_rates.py).

Pipeline per settlement (8h grid, funding_time = timestamptz/UTC):
  1. Cross-section over all coins of settlement → percentile rank.
  2. Events: percentile >= 0.95 → SHORT candidate, <= 0.05 → LONG candidate
     (gates from core.research_features, live mirrored in bot 31).
  3. Dedup per symbol+side: 24h minimum spacing (mirror of live cooldown).
  4. Statistics features from own settlement history (funding_stats —
     shared builder), market context floor-1 (no lookahead).
  5. Geometry: calculate_smart_targets; label: simulate_exit, horizon 7 days.

Known, intentional residual skew: live gated bot 31 uses the CURRENT rate
(premiumIndex), here the SETTLED rate — same source, one settlement offset.

V2 / FMR2 (K4, docs/NEW_IDEAS_BOTS.md § "FMR2 — own exit path"):
  --label-version v2 swaps step 5: instead of first-touch TP/SL
  (simulate_exit) runs simulate_normalization_exit — hold until funding
  extreme NORMALISES (fmr2_funding_normalized) or time-stop (9 settlements),
  label = sign of net PnL at exit price of settlement candle. Hard catastrophe
  SL remains as first-touch safety net. Output → fmr2_events.jsonl.

Example:
  python tools/fmr1_build_dataset.py                          # V1 (FMR1 stock)
  python tools/fmr1_build_dataset.py --limit-symbols 15
  python tools/fmr1_build_dataset.py --label-version v2       # V2 (FMR2, K4)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd

# REPO_ROOT MUST be on sys.path before the first tools-/core import
# (chicken-egg; spec-review fix 2026-07-06, pattern tools/aim2_build_dataset.py).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.research_dataset_common import (  # noqa: E402
    MIN_WINDOW,
    REPLAY_DIR,
    WINDOW_CANDLES,
    df_query,
    floor_idx,
    join_is_stale,
    load_candles_ctx,
    log,
    set_low_priority,
)

from core.database import get_db_connection  # noqa: E402
from core.research_features import (  # noqa: E402
    FMR1_HISTORY_SETTLEMENTS,
    FMR1_LONG_PCTL,
    FMR1_SHORT_PCTL,
    FMR2_TIME_STOP_SETTLEMENTS,
    build_fmr1_row,
    fmr2_catastrophe_sl,
    fmr2_funding_normalized,
    funding_stats,
)
from core.trade_utils import calculate_smart_targets  # noqa: E402
from tools.walkforward_sim import FEE_PER_SIDE, simulate_exit  # noqa: E402

SINCE_DEFAULT = "2026-02-25"
HORIZON_CANDLES = 7 * 24
DEDUP_HOURS = 24              # Mirror of live cooldown (bot 31)
N_PUBLISHED = 3
MIN_CROSS_SECTION = 50        # Discard settlements with fewer coins
MIN_HISTORY = 10              # funding_stats minimum history


def load_funding(conn, since: str) -> pd.DataFrame:
    """Full settlement history (incl. lead time for statistics features)."""
    df = df_query(
        conn,
        """
        SELECT symbol, funding_time, funding_rate
        FROM funding_rates
        WHERE funding_time >= %s::timestamptz - INTERVAL '45 days'
        ORDER BY funding_time ASC
        """,
        (since,),
    )
    if df.empty:
        raise SystemExit("funding_rates is empty — first run tools/backfill_funding_rates.py")
    df["funding_time"] = pd.to_datetime(df["funding_time"], utc=True).dt.tz_localize(None)
    df["symbol"] = df["symbol"].astype(str).str.upper()
    df = df[df["symbol"].str.endswith("USDT")]
    df["funding_rate"] = pd.to_numeric(df["funding_rate"], errors="coerce")
    return df.dropna(subset=["funding_rate"]).reset_index(drop=True)


def build_events(fund: pd.DataFrame, since: str) -> pd.DataFrame:
    """Cross-section per settlement → extreme events with percentile."""
    since_ts = pd.Timestamp(since)
    events = []
    for ft, g in fund.groupby("funding_time", sort=True):
        if ft < since_ts or len(g) < MIN_CROSS_SECTION:
            continue
        pctl = g["funding_rate"].rank(pct=True)
        for side, mask in (("SHORT", pctl >= FMR1_SHORT_PCTL), ("LONG", pctl <= FMR1_LONG_PCTL)):
            for i in g.index[mask]:
                events.append({
                    "symbol": g.at[i, "symbol"], "ts": ft, "direction": side,
                    "rate": float(g.at[i, "funding_rate"]), "pctl": float(pctl.at[i]),
                })
    ev = pd.DataFrame(events).sort_values("ts").reset_index(drop=True)

    keep, last_ts = [], {}
    for row in ev.itertuples():
        key = (row.symbol, row.direction)
        prev = last_ts.get(key)
        ok = prev is None or (row.ts - prev).total_seconds() >= DEDUP_HOURS * 3600
        keep.append(ok)
        if ok:
            last_ts[key] = row.ts
    return ev[pd.Series(keep, index=ev.index)].reset_index(drop=True)


def simulate_normalization_exit(
    direction: str,
    entry_price: float,
    times: np.ndarray,
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    entry_idx: int,
    f_ts: np.ndarray,
    f_rates: np.ndarray,
    cs_pctl: np.ndarray,
    ev_pos: int,
    fee_per_side: float = FEE_PER_SIDE,
) -> dict:
    """V2 label (FMR2, K4): hold the mean-reversion trade until funding
    extreme NORMALISES (core.research_features.fmr2_funding_normalized) OR until
    time-stop (FMR2_TIME_STOP_SETTLEMENTS = 9 settlements / 3 days) — exit price =
    close of settlement candle. This is the core difference from V1: NO first-touch
    TP/SL (that was the FMR1 bug, Report 15 V2 diagnosis). Hard catastrophe SL
    (fmr2_catastrophe_sl) remains as first-touch safety net.

    times/highs/lows/closes: 1h candles of symbol (naive UTC datetime64, ASC).
    entry_idx: index of entry candle (last closed candle before event settlement);
      walk starts at entry_idx+1 (no lookahead, R1).
    f_ts/f_rates/cs_pctl: full settlement history of symbol (ASC); ev_pos = position
      of event settlement in this history. funding_z_30d is recomputed as-of per
      settlement — identical formula to funding_stats (cur vs. last FMR1_HISTORY_SETTLEMENTS rows).
    """
    is_long = direction.upper() == "LONG"
    sl = fmr2_catastrophe_sl(direction, entry_price)
    n = len(times)
    n_settle = len(f_ts)
    settlements = 0
    next_j = ev_pos + 1  # next settlement position AFTER entry settlement
    exit_price: float | None = None
    exit_reason: str | None = None
    exit_time = None

    i = entry_idx + 1
    while i < n:
        # 1) Catastrophe SL first (touch-based, conservative — liquidation is touch).
        if (lows[i] <= sl) if is_long else (highs[i] >= sl):
            exit_price, exit_reason, exit_time = float(sl), "catastrophe_sl", times[i]
            break
        # 2) Settlement(s) reached? 1h candles & 8h settlements lie on the grid;
        #    a data gap can skip multiple settlements → while, not if.
        while next_j < n_settle and times[i] >= f_ts[next_j]:
            settlements += 1
            cur = float(f_rates[next_j]) * 1e4
            j0 = max(0, next_j - (FMR1_HISTORY_SETTLEMENTS - 1))
            hist = f_rates[j0 : next_j + 1] * 1e4
            std = float(hist.std())
            z = (cur - float(hist.mean())) / std if std > 0 else float("nan")
            if fmr2_funding_normalized(direction, float(cs_pctl[next_j]), z):
                exit_price, exit_reason, exit_time = float(closes[i]), "normalized", times[i]
                break
            if settlements >= FMR2_TIME_STOP_SETTLEMENTS:
                exit_price, exit_reason, exit_time = float(closes[i]), "time_stop", times[i]
                break
            next_j += 1
        if exit_reason is not None:
            break
        i += 1

    if exit_price is None:
        return {"exit_reason": "open_at_end", "exit_price": None, "exit_time": None,
                "net_pnl_pct": None, "settlements": settlements, "risk_pct": None}

    gross = (exit_price - entry_price) / entry_price if is_long else (entry_price - exit_price) / entry_price
    net = (gross - 2.0 * fee_per_side) * 100.0
    risk = abs(entry_price - sl) / entry_price * 100.0 if entry_price else 0.0
    return {"exit_reason": exit_reason, "exit_price": exit_price,
            "exit_time": str(exit_time), "net_pnl_pct": round(net, 4),
            "settlements": settlements, "risk_pct": round(risk, 4)}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=SINCE_DEFAULT)
    ap.add_argument("--label-version", choices=("v1", "v2"), default="v1",
                    help="v1 = FMR1 first-touch TP/SL (stock); v2 = FMR2 "
                         "normalisation/timeout exit (K4).")
    ap.add_argument("--out", default=None)
    ap.add_argument("--limit-symbols", type=int, default=0)
    args = ap.parse_args()
    if args.out is None:
        fname = "fmr2_events.jsonl" if args.label_version == "v2" else "fmr1_events.jsonl"
        args.out = os.path.join(REPLAY_DIR, fname)

    set_low_priority()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    t0 = time.time()

    conn = get_db_connection()
    fund = load_funding(conn, args.since)
    log(f"Funding rows: {len(fund)} over {fund['symbol'].nunique()} symbols")
    ev = build_events(fund, args.since)
    log(f"Extreme events after dedup: {len(ev)} "
        f"(SHORT {int((ev['direction'] == 'SHORT').sum())} / LONG {int((ev['direction'] == 'LONG').sum())})")

    # Cross-sectional percentile per settlement over ALL coins — same size as
    # build_events (row.pctl), here made available for V2 hold phase per symbol
    # (as-of recomputed at each settlement).
    fund["cs_pctl"] = fund.groupby("funding_time")["funding_rate"].rank(pct=True)

    # Settlement history per symbol as arrays (for funding_stats until event time
    # and — V2 — for normalization walk).
    hist: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
    for sym, g in fund.groupby("symbol", sort=False):
        hist[sym] = (
            g["funding_time"].values.astype("datetime64[ns]"),
            g["funding_rate"].to_numpy(dtype=np.float64),
            g["cs_pctl"].to_numpy(dtype=np.float64),
        )

    symbols = list(ev["symbol"].drop_duplicates())
    if args.limit_symbols:
        symbols = symbols[: args.limit_symbols]

    stats = {k: 0 for k in ("written", "wins", "open_end", "no_candles", "no_window",
                            "stale_join", "geometry_fail", "short_history")}
    with open(args.out, "w", encoding="utf-8") as fh:
        for i, sym in enumerate(symbols, 1):
            df = load_candles_ctx(conn, sym, args.since)
            sym_ev = ev[ev["symbol"] == sym]
            if df is None or len(df) < MIN_WINDOW:
                stats["no_candles"] += len(sym_ev)
                continue
            times = df["open_time"].values.astype("datetime64[ns]")
            highs = df["high"].to_numpy(dtype=np.float64)
            lows = df["low"].to_numpy(dtype=np.float64)
            closes = df["close"].to_numpy(dtype=np.float64)
            f_ts, f_rates, f_cs = hist[sym]

            for row in sym_ev.itertuples():
                ts64 = np.datetime64(row.ts)
                idx = floor_idx(times, row.ts)
                if idx < MIN_WINDOW:
                    stats["no_window"] += 1
                    continue
                if join_is_stale(times, idx, row.ts):
                    stats["stale_join"] += 1
                    continue
                # History up to AND INCLUDING the event settlement (no lookahead).
                hi = int(np.searchsorted(f_ts, ts64, side="right"))
                if hi < MIN_HISTORY:
                    stats["short_history"] += 1
                    continue
                try:
                    f_stats = funding_stats(list(f_rates[:hi]))
                    entry_close = float(closes[idx])
                    feats = build_fmr1_row(f_stats, row.pctl, row.direction, df, idx)
                    if args.label_version == "v2":
                        # FMR2: entry = close of the entry candle (no smart-target
                        # limit — V2 holds until normalisation, no TP ladder).
                        if entry_close <= 0:
                            raise ValueError("degenerate entry")
                        ev_pos = int(np.searchsorted(f_ts, ts64, side="left"))
                        res = simulate_normalization_exit(
                            row.direction, entry_close, times, highs, lows, closes,
                            entry_idx=idx, f_ts=f_ts, f_rates=f_rates, cs_pctl=f_cs,
                            ev_pos=ev_pos,
                        )
                        entry_out, sl_out, targets_out = entry_close, None, None
                    else:
                        # V1 (FMR1 stock): smart-target geometry + first-touch.
                        win = df.iloc[max(0, idx - WINDOW_CANDLES + 1): idx + 1]
                        setup = calculate_smart_targets(None, sym, row.direction, entry_close, df=win)
                        entry1 = float(setup["entry1"])
                        sl = float(setup["sl"])
                        targets = [float(t) for t in setup["targets"][:N_PUBLISHED]]
                        if not targets or sl <= 0 or entry1 <= 0:
                            raise ValueError("degenerate geometry")
                        end = min(idx + 1 + HORIZON_CANDLES, len(times))
                        res = simulate_exit(
                            times[:end], highs[:end], lows[:end], closes[:end],
                            start_idx=idx + 1, direction=row.direction, entry=entry1, sl=sl,
                            targets=targets, n_published=len(targets),
                        )
                        entry_out, sl_out, targets_out = entry1, sl, targets
                except ValueError:
                    stats["geometry_fail"] += 1
                    continue
                except Exception:
                    stats["geometry_fail"] += 1
                    continue

                if args.label_version == "v2":
                    # label = sign of the realised net PnL at the normalisation/
                    # timeout exit (NOT first-touch TP/SL — the FMR1 bug).
                    net = res.get("net_pnl_pct")
                    label = None if net is None else int(net > 0)
                else:
                    label = res.get("outcome_tp1")
                    if res.get("exit_reason") == "open_at_end":
                        label = None

                fh.write(json.dumps({
                    "symbol": sym, "ts": pd.Timestamp(row.ts).isoformat(),
                    "direction": row.direction, "weight": 1.0,
                    "entry": entry_out, "sl": sl_out, "targets": targets_out,
                    "label": label, "net_pnl_pct": res.get("net_pnl_pct"),
                    "exit_reason": res.get("exit_reason"), "risk_pct": res.get("risk_pct"),
                    "settlements": res.get("settlements"),
                    "features": feats,
                }) + "\n")
                stats["written"] += 1
                stats["wins"] += 1 if label == 1 else 0
                stats["open_end"] += 1 if label is None else 0

            if i % 25 == 0 or i == len(symbols):
                closed = stats["written"] - stats["open_end"]
                wr = stats["wins"] / closed * 100 if closed else 0.0
                log(f"{i}/{len(symbols)} symbols | written {stats['written']} "
                    f"(WR closed: {wr:.1f}%) | {time.time() - t0:.0f}s")
    conn.close()
    log(f"DONE -> {args.out}")
    log(json.dumps(stats))


if __name__ == "__main__":
    main()
