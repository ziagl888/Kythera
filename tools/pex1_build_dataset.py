"""
tools/pex1_build_dataset.py — training events + replay labels for PEX1
"Pump-Exhaustion-Short" (Report 15, S6). Runs on the VPS (Step 2).

Pipeline per event (pump_dump_events, gates mirrored from live bot 30:
volume_ratio >= 5, price_change_60s >= +1.5):
  1. TZ: spike_time offset is measured against the wall clock (session TZ of
     the detector unknown) → conversion to UTC.
  2. Dedup per symbol: 4h minimum spacing (mirror of the live cooldown).
  3. floor-1 join onto the last CLOSED 1h candle (no lookahead).
  4. Geometry: calculate_smart_targets SHORT on the candle window — exactly
     what bot 30 computes when posting.
  5. Label: simulate_exit (first-touch, SL-first, fees), horizon 7 days.
  6. Features: core.research_features.build_pex1_row (shared builder).

Example:
  python tools/pex1_build_dataset.py                 # full run
  python tools/pex1_build_dataset.py --limit-symbols 15   # smoke test
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time

import numpy as np
import pandas as pd

# REPO_ROOT MUST be on sys.path before the first tools/core import —
# otherwise the insert in research_dataset_common would never execute
# (chicken-and-egg; spec review fix 2026-07-06, pattern tools/aim2_build_dataset.py).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.time import legacy_naive_to_utc  # noqa: E402
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
from core.research_features import PEX1_MIN_PUMP_PCHG_60S, PEX1_MIN_VOL_RATIO, build_pex1_row  # noqa: E402
from core.trade_utils import calculate_smart_targets  # noqa: E402
from tools.walkforward_sim import simulate_exit  # noqa: E402

SINCE_DEFAULT = "2026-02-25"
HORIZON_CANDLES = 7 * 24      # exhaustion thesis lives hours to days
DEDUP_HOURS = 4               # mirror of the live cooldown (bot 30)
N_PUBLISHED = 3


def detect_offset_h(conn) -> int:
    row = df_query(conn, "SELECT MAX(spike_time) AS m FROM pump_dump_events")["m"].iloc[0]
    if pd.isna(row):
        return 0
    row_ts = pd.Timestamp(row)
    if row_ts.tzinfo is not None:
        # timestamptz column (actual state since measurement 2026-07-06): PG returns
        # aware UTC — no offset heuristic needed, conversion is handled by
        # spike_time_to_utc via the tz-aware branch.
        return 0
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    return int(np.clip(round((row_ts - now).total_seconds() / 3600.0), -12, 12))


def spike_time_to_utc(series: pd.Series, offset_h: int) -> pd.Series:
    """spike_time → naive UTC. A constant offset over months would be DST-blind
    (review fix 2026-07-06: pre-DST events would shift 1h → candle BEFORE the
    event in the label). Offset 2/3h ⇒ domain is the legacy writer TZ
    (Europe/Bucharest) → convert DST-aware; 0 ⇒ already UTC; anything else:
    constant shift with warning (unknown domain).

    Live actual state (read-only measured 2026-08-01, T-2026-KYT-9050-005):
    ``pump_dump_events.spike_time`` is ``timestamp WITH time zone`` — the
    aware branch below applies, the offset heuristic is already dead code for
    this table today and measures 0 anyway after the R3 flip. The naive branch
    remains as a fallback for older dumps; it goes through the same central
    read path as all other legacy columns (core.time, docs/UTC_POLICY.md §6)."""
    # Check awareness on the RAW value, not the parsed column: timestamptz
    # across a DST boundary (e.g. 2026-03-29 EET→EEST) returns MIXED
    # offsets (+02/+03). pd.to_datetime without utc=True then fixes the offset
    # of the first row and coerces all deviating rows to NaT — the
    # EPD2 run on 2026-07-07 lost ALL events after the DST change this way.
    sample = next((v for v in series if v is not None and not pd.isna(v)), None)
    if sample is not None and getattr(sample, "tzinfo", None) is not None:
        # timestamptz: aware → utc=True handles mixed offsets → naive UTC.
        s = pd.to_datetime(series, errors="coerce", utc=True)
        return s.dt.tz_localize(None)
    s = pd.to_datetime(series, errors="coerce")
    if offset_h == 0:
        return s
    if offset_h in (2, 3):
        # Domain MEASURED HERE (detect_offset_h), not assumed — hence the
        # only sanctioned assume_legacy call; the DST recipe itself
        # lives centrally in core.time (one place, not six).
        return legacy_naive_to_utc(s, assume_legacy=True)
    log(f"WARNING: spike_time offset {offset_h:+d}h does not match any known TZ domain — "
        f"constant shift (DST-blind).")
    return s - pd.Timedelta(hours=offset_h)


def load_events(conn, since: str, offset_h: int) -> pd.DataFrame:
    ev = df_query(
        conn,
        """
        SELECT symbol, spike_time, volume_ratio, price_change_60s, buy_pressure, volatility
        FROM pump_dump_events
        WHERE volume_ratio >= %s AND price_change_60s >= %s
          AND spike_time > %s::timestamp
        ORDER BY spike_time ASC
        """,
        # Coarse SQL pre-filter with 1 day margin; exact since-cut after the
        # TZ conversion in pandas.
        (PEX1_MIN_VOL_RATIO, PEX1_MIN_PUMP_PCHG_60S, since),
    )
    ev["ts"] = spike_time_to_utc(ev["spike_time"], offset_h)
    ev["symbol"] = ev["symbol"].astype(str).str.upper()
    ev = ev[ev["symbol"].str.endswith("USDT")].dropna(subset=["ts"])
    ev = ev[ev["ts"] >= pd.Timestamp(since)]

    # Dedup: 4h minimum spacing per symbol (first spike wins — like the live cooldown).
    keep, last_ts = [], {}
    for row in ev.itertuples():
        prev = last_ts.get(row.symbol)
        ok = prev is None or (row.ts - prev).total_seconds() >= DEDUP_HOURS * 3600
        keep.append(ok)
        if ok:
            last_ts[row.symbol] = row.ts
    return ev[pd.Series(keep, index=ev.index)].reset_index(drop=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=SINCE_DEFAULT)
    ap.add_argument("--out", default=os.path.join(REPLAY_DIR, "pex1_events.jsonl"))
    ap.add_argument("--limit-symbols", type=int, default=0)
    args = ap.parse_args()

    set_low_priority()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    t0 = time.time()

    conn = get_db_connection()
    offset_h = detect_offset_h(conn)
    log(f"spike_time offset: {offset_h:+d}h vs UTC")
    ev = load_events(conn, args.since, offset_h)
    log(f"Events after gates + dedup: {len(ev)} across {ev['symbol'].nunique()} symbols")

    symbols = list(ev["symbol"].drop_duplicates())
    if args.limit_symbols:
        symbols = symbols[: args.limit_symbols]

    stats = {k: 0 for k in ("written", "wins", "open_end", "no_candles", "no_window",
                            "stale_join", "geometry_fail")}
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

            for row in sym_ev.itertuples():
                idx = floor_idx(times, row.ts)
                if idx < MIN_WINDOW:
                    stats["no_window"] += 1
                    continue
                if join_is_stale(times, idx, row.ts):
                    stats["stale_join"] += 1
                    continue
                # Entry = spike price estimate, NOT the pre-pump close
                # (review fix HIGH 2026-07-06): bot 30 enters live POST-pump
                # (live_price after the spike). The pre-pump close as entry would have
                # produced pump-correlated deflated labels (the pump itself
                # would have torn the simulated SL). Estimator: last close × (1 + 60s move).
                entry_est = float(closes[idx]) * (1.0 + float(row.price_change_60s) / 100.0)
                try:
                    win = df.iloc[max(0, idx - WINDOW_CANDLES + 1): idx + 1]
                    setup = calculate_smart_targets(None, sym, "SHORT", entry_est, df=win)
                    entry1 = float(setup["entry1"])
                    sl = float(setup["sl"])
                    targets = [float(t) for t in setup["targets"][:N_PUBLISHED]]
                    if not targets or sl <= 0 or entry1 <= 0:
                        raise ValueError("degenerate geometry")
                    # Replay from idx+2: the event candle (idx+1) contains the
                    # pump run-up BEFORE our entry — wick-aware first-touch
                    # would falsely count it as an SL breach. Conservative
                    # (fast TP hits within the same hour are also dropped);
                    # aim2 precedent: --skip-entry-hour.
                    end = min(idx + 2 + HORIZON_CANDLES, len(times))
                    res = simulate_exit(
                        times[:end], highs[:end], lows[:end], closes[:end],
                        start_idx=idx + 2, direction="SHORT", entry=entry1, sl=sl,
                        targets=targets, n_published=len(targets),
                    )
                    event = {
                        "volume_ratio": row.volume_ratio,
                        "price_change_60s": row.price_change_60s,
                        "buy_pressure": row.buy_pressure,
                        "volatility": row.volatility,
                    }
                    feats = build_pex1_row(event, df, idx)
                except Exception:
                    stats["geometry_fail"] += 1
                    continue

                label = res.get("outcome_tp1")
                if res.get("exit_reason") == "open_at_end":
                    label = None  # Report 13 rule: do not label open trades
                fh.write(json.dumps({
                    "symbol": sym, "ts": pd.Timestamp(row.ts).isoformat(),
                    "direction": "SHORT", "weight": 1.0,
                    "entry": entry1, "sl": sl, "targets": targets,
                    "label": label, "net_pnl_pct": res.get("net_pnl_pct"),
                    "exit_reason": res.get("exit_reason"), "risk_pct": res.get("risk_pct"),
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
