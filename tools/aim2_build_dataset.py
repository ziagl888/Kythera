"""
tools/aim2_build_dataset.py — training events + replay labels + features for AIM2.

Builds the complete training material for the new master meta-model
(docs/AIM2_DESIGN.md) and writes one JSONL line per event to
<staging>/replay/aim2_events.jsonl.

Pipeline per event (source signal):
  1. Timestamp: ml_predictions_master/*_trades_master write naive UTC since
     the R3 flip (T-2026-KYT-9050-005). How the rows BEFORE that are read
     (backfill vs. cutover constant) is decided by core.time —
     docs/UTC_POLICY.md §6. The builder no longer converts anything itself.
  2. floor-1 join: last CLOSED 1h candle before the event (no lookahead —
     the round('1h') bug of the AIM1 trainer is thereby structurally dead).
  3. Geometry as-of: calculate_smart_targets on the candle window up to
     that candle — exactly what bot 15 would compute when posting.
  4. Label: simulate_exit from tools/walkforward_sim.py (wick-aware first-touch,
     SL-first, fees, monitor trailing), horizon cap 14 days.
  5. Features: exclusively core.aim2_features.build_feature_row
     (shared builder = serving parity).

Conv signals are deterministically undersampled (md5 hash, reproducible),
because FIFO/Volume would otherwise make up 80% of the dataset; the swarm
context is ALWAYS computed on the full event stream (sampling must not
change the world, only the training selection).

Example:
  python tools/aim2_build_dataset.py                # full build
  python tools/aim2_build_dataset.py --limit-symbols 15   # smoke test
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys
import time

import numpy as np
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from core.aim2_features import (  # noqa: E402
    ATR_COLS,
    CONV_CONFIDENCE_MAPPING,
    MARKET_ABS_COLS,
    MARKET_PRICE_COLS,
    TRAIL_WIN_SQL,
    TRAIL_WINDOW_DAYS,
    build_feature_row,
)
from core.aim2_topn import MODEL_TAG as TOPN_TAG  # noqa: E402
from core.candles import read_candles_with_indicators  # noqa: E402
from core.database import get_db_connection  # noqa: E402
from core.trade_utils import calculate_smart_targets  # noqa: E402
from tools.research_dataset_common import candles_window_start  # noqa: E402
from tools.research_dataset_common import to_utc_naive as _shared_to_utc_naive  # noqa: E402
from tools.walkforward_sim import simulate_exit  # noqa: E402  (import only — file belongs to the ABR1 rework)

STAGING_DIR = os.getenv("KYTHERA_STAGING_DIR", r"C:\Users\Michael\Documents\_X\staging_models")
REPLAY_DIR = os.getenv("KYTHERA_REPLAY_DIR", os.path.join(STAGING_DIR, "replay"))

SINCE_DEFAULT = "2026-02-25"
HORIZON_CANDLES = 14 * 24          # 14 days of 1h
WINDOW_CANDLES = 500               # smart-targets window
MIN_WINDOW = 60
MAX_JOIN_STALENESS_H = 3           # candle gap → discard event
N_PUBLISHED = 3                    # bot 15 posts targets[:3]

# Deterministic conv sampling in % (md5 over "strategy|id")
CONV_SAMPLE_PCT = {
    "Fast In And Out": 25,
    "Volume Indicator": 35,
}

TF_SUFFIX_RE = re.compile(r"_\d+[mhdwM]$")

IND_COLS = MARKET_PRICE_COLS + MARKET_ABS_COLS + ATR_COLS + ["trend_direction"]


def log(msg: str) -> None:
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def set_low_priority() -> None:
    try:
        import psutil

        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass


def to_utc_naive(series: pd.Series) -> pd.Series:
    """Signal/close times as naive UTC — ONE path with the research builders.

    The fixed Bucharest→UTC step went away with the R3 flip
    (T-2026-KYT-9050-005): the writers stamp UTC. Reading the history depends on
    core.time.R3_CUTOVER_UTC (docs/UTC_POLICY.md §6); the DST spring gap is
    pushed forward there, the ambiguous autumn hour becomes NaT."""
    return _shared_to_utc_naive(series)


def df_query(conn, sql: str, params=None) -> pd.DataFrame:
    with conn.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()
        cols = [d[0] for d in cur.description]
    return pd.DataFrame(rows, columns=cols)


def keep_sampled(strategy: str, event_id) -> bool:
    pct = CONV_SAMPLE_PCT.get(strategy, 100)
    if pct >= 100:
        return True
    h = int(hashlib.md5(f"{strategy}|{event_id}".encode()).hexdigest(), 16)
    return (h % 100) < pct


# ─────────────────────────────────────────────────────────────────────────────
# 1) EVENTS
# ─────────────────────────────────────────────────────────────────────────────
def load_events(conn, since: str) -> pd.DataFrame:
    # Meta-gate outputs are not a base signal: AIM1 (dead), AIM2 (posting since
    # 06.07.) and AIM2-TOPN (T-2026-CU-9050-051) are excluded, otherwise an
    # AIM2 retrain would label its own gate decisions as training events
    # (F6 self-feedback). Identical to the serving definition in
    # 15_ai_master_bot.load_signal_stream — the AIM2_DESIGN.md §3 invariant
    # "identical definition as in the trainer" (T-2026-CU-9050-065).
    ai = df_query(
        conn,
        """
        SELECT id, model_name AS source, time, coin, direction, entry, confidence
        FROM ml_predictions_master
        WHERE posted = true AND model_name NOT IN ('AIM1', 'AIM2', %s) AND time > %s
        """,
        (TOPN_TAG, since),
    )
    ai["source_type"] = "ai"

    conv = df_query(
        conn,
        """
        SELECT id, strategy AS source, time, coin, direction, entry, NULL::real AS confidence
        FROM active_trades_master WHERE time > %s
        UNION ALL
        SELECT id, strategy AS source, time, coin, direction, entry, NULL::real AS confidence
        FROM closed_trades_master WHERE time > %s
        """,
        (since, since),
    )
    conv["source_type"] = "conv"

    ev = pd.concat([ai, conv], ignore_index=True)
    ev["ts"] = to_utc_naive(ev["time"])
    ev = ev.dropna(subset=["ts", "coin", "direction"])
    ev["symbol"] = ev["coin"].astype(str).str.upper().str.replace(TF_SUFFIX_RE, "", regex=True)
    ev = ev[ev["symbol"].str.endswith("USDT")]
    ev["direction"] = ev["direction"].astype(str).str.upper()
    ev = ev[ev["direction"].isin(["LONG", "SHORT"])]
    ev["entry"] = pd.to_numeric(ev["entry"], errors="coerce")
    ev["confidence"] = pd.to_numeric(ev["confidence"], errors="coerce")
    ev = ev.sort_values("ts").reset_index(drop=True)

    ev["sampled"] = [
        keep_sampled(s, i) if st == "conv" else True
        for s, i, st in zip(ev["source"], ev["id"], ev["source_type"])
    ]
    ev["weight"] = [
        100.0 / CONV_SAMPLE_PCT.get(s, 100) if st == "conv" else 1.0
        for s, st in zip(ev["source"], ev["source_type"])
    ]
    return ev


# ─────────────────────────────────────────────────────────────────────────────
# 2) CONTEXT STRUCTURES (swarm, regime, trailing WR)
# ─────────────────────────────────────────────────────────────────────────────
def build_swarm_index(ev: pd.DataFrame) -> dict:
    """Full event stream per symbol (BEFORE sampling) for the swarm features."""
    idx = {}
    for sym, g in ev.groupby("symbol", sort=False):
        g = g.sort_values("ts")
        idx[sym] = (
            g["ts"].values.astype("datetime64[ns]"),
            (g["direction"].values == "LONG").astype(np.int8),
            g["source"].values,
        )
    return idx


def swarm_stats(index: dict, symbol: str, ts64, direction: str) -> dict:
    entry = index.get(symbol)
    out = {
        "total_5d": 0, "long_5d": 0, "short_5d": 0, "latest_age_h": 120.0,
        "confl_same_dir_4h": 0, "distinct_src_same_dir_4h": 0,
    }
    if entry is None:
        return out
    ts_arr, is_long, src = entry
    hi = int(np.searchsorted(ts_arr, ts64, side="left"))  # strictly < ts → excludes the event itself (F6)
    lo5 = int(np.searchsorted(ts_arr, ts64 - np.timedelta64(5, "D"), side="left"))
    if hi <= lo5:
        return out
    seg_long = is_long[lo5:hi]
    out["total_5d"] = hi - lo5
    out["long_5d"] = int(seg_long.sum())
    out["short_5d"] = out["total_5d"] - out["long_5d"]
    out["latest_age_h"] = float((ts64 - ts_arr[hi - 1]) / np.timedelta64(1, "h"))
    lo4 = int(np.searchsorted(ts_arr, ts64 - np.timedelta64(4, "h"), side="left"))
    if hi > lo4:
        want = 1 if direction == "LONG" else 0
        mask = is_long[lo4:hi] == want
        out["confl_same_dir_4h"] = int(mask.sum())
        out["distinct_src_same_dir_4h"] = int(len(set(src[lo4:hi][mask])))
    return out


def load_regime(conn) -> tuple[np.ndarray, list[dict]]:
    df = df_query(
        conn,
        """
        SELECT ts, regime, alt_context, confidence, confidence_btc, confidence_alt,
               btc_return_1h, btc_return_4h, btc_atr_1h_pct, btc_atr_4h_pct, btcdom_return_24h
        FROM regime_history ORDER BY ts
        """,
    )
    ts = pd.to_datetime(df["ts"]).values.astype("datetime64[ns]")  # already naive UTC (Step 2)
    return ts, df.to_dict("records")


def regime_at(r_ts: np.ndarray, r_rows: list[dict], ts64) -> tuple[dict | None, float]:
    i = int(np.searchsorted(r_ts, ts64, side="right")) - 1
    if i < 0:
        return None, 360.0
    age_min = float((ts64 - r_ts[i]) / np.timedelta64(1, "m"))
    return r_rows[i], age_min


def load_trail_index(conn, since: str) -> dict:
    """closed_ai_signals deduplicated → per-model sorted (close_time, win) arrays.
    Win semantics = TRAIL_WIN_SQL (identical in serving)."""
    df = df_query(
        conn,
        f"""
        SELECT model, close_time, bool_or({TRAIL_WIN_SQL}) AS win
        FROM closed_ai_signals
        WHERE close_time > %s::timestamp - INTERVAL '{TRAIL_WINDOW_DAYS + 5} days'
        GROUP BY model, symbol, direction, open_time, close_time
        """,
        (since,),
    )
    df["ct"] = to_utc_naive(df["close_time"])
    df = df.dropna(subset=["ct"]).sort_values("ct")
    out = {}
    for model, g in df.groupby("model", sort=False):
        wins = g["win"].astype(int).to_numpy()
        out[model] = (g["ct"].values.astype("datetime64[ns]"), np.concatenate(([0], np.cumsum(wins))))
    return out


def trail_wr(index: dict, model: str, ts64) -> tuple[float, int]:
    entry = index.get(model)
    if entry is None:
        return 0.5, 0
    ct, cum = entry
    hi = int(np.searchsorted(ct, ts64, side="left"))
    lo = int(np.searchsorted(ct, ts64 - np.timedelta64(TRAIL_WINDOW_DAYS, "D"), side="left"))
    n = hi - lo
    if n <= 0:
        return 0.5, 0
    return float(cum[hi] - cum[lo]) / n, n


# ─────────────────────────────────────────────────────────────────────────────
# 3) LABELING JE SYMBOL
# ─────────────────────────────────────────────────────────────────────────────
def load_candles(conn, symbol: str, since: str) -> pd.DataFrame | None:
    # Via core.candles: CLOSED 1h candles + indicator join (include_forming
    # =False). process_symbol trims via searchsorted-1 to the last
    # closed candle — the forming tail row was never part of the contract.
    try:
        df = read_candles_with_indicators(
            conn,
            symbol,
            "1h",
            start=candles_window_start(since, 30),
            include_forming=False,
            candle_columns=("open_time", "open", "high", "low", "close", "volume"),
            indicator_columns=IND_COLS,
        )
    except Exception:
        conn.rollback()
        return None
    if df.empty:
        return None
    ot = pd.to_datetime(df["open_time"], utc=True)
    df["open_time"] = ot.dt.tz_localize(None)
    for c in df.columns:
        if c not in ("open_time", "trend_direction"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["high", "low", "close"]).reset_index(drop=True)


def process_symbol(conn, symbol: str, events: pd.DataFrame, swarm_idx, r_ts, r_rows,
                   trail_idx, out_fh, stats: dict, skip_entry_hour: bool = False) -> None:
    df = load_candles(conn, symbol, SINCE_DEFAULT)
    if df is None or len(df) < MIN_WINDOW:
        stats["no_candles"] += len(events)
        return

    times = df["open_time"].values.astype("datetime64[ns]")
    highs = df["high"].to_numpy(dtype=np.float64)
    lows = df["low"].to_numpy(dtype=np.float64)
    closes = df["close"].to_numpy(dtype=np.float64)
    ind_arrays = {c: df[c].to_numpy() for c in IND_COLS}

    # Events of the same hour+direction share geometry, replay and market row
    # (FIFO bursts!) — compute once, use often.
    label_cache: dict[tuple[int, str], tuple | None] = {}

    for ev in events.itertuples():
        ts64 = np.datetime64(ev.ts)
        floor64 = np.datetime64(pd.Timestamp(ev.ts).floor("h"))
        idx = int(np.searchsorted(times, floor64, side="left")) - 1  # last CLOSED candle
        if idx < MIN_WINDOW:
            stats["no_window"] += 1
            continue
        if (floor64 - times[idx]) / np.timedelta64(1, "h") > MAX_JOIN_STALENESS_H:
            stats["stale_join"] += 1
            continue

        # Conservative mode: skip the signal-hour candle — its high/low
        # contains up to 1h of price movement BEFORE the signal (label lookahead probe).
        start_off = 2 if skip_entry_hour else 1
        cache_key = (idx, ev.direction)
        if cache_key not in label_cache:
            entry_close = float(closes[idx])
            if entry_close <= 0:
                label_cache[cache_key] = None
            else:
                try:
                    win = df.iloc[max(0, idx - WINDOW_CANDLES + 1): idx + 1]
                    setup = calculate_smart_targets(None, symbol, ev.direction, entry_close, df=win)
                    entry1 = float(setup["entry1"])
                    sl = float(setup["sl"])
                    targets = [float(t) for t in setup["targets"][:N_PUBLISHED]]
                    if not targets or sl <= 0 or entry1 <= 0:
                        raise ValueError("degenerate geometry")
                    end = min(idx + start_off + HORIZON_CANDLES, len(times))
                    res = simulate_exit(
                        times[:end], highs[:end], lows[:end], closes[:end],
                        start_idx=idx + start_off, direction=ev.direction, entry=entry1, sl=sl,
                        targets=targets, n_published=len(targets),
                    )
                    market_row = {c: ind_arrays[c][idx] for c in IND_COLS}
                    label_cache[cache_key] = (entry_close, entry1, sl, targets, res, market_row)
                except Exception:
                    label_cache[cache_key] = None

        cached = label_cache[cache_key]
        if cached is None:
            stats["geometry_fail"] += 1
            continue
        entry_close, entry1, sl, targets, res, market_row = cached

        label = res.get("outcome_tp1")
        if res.get("exit_reason") == "open_at_end":
            label = None  # Report-13 rule: do not label open trades
        regime_row, regime_age = regime_at(r_ts, r_rows, ts64)
        swarm = swarm_stats(swarm_idx, symbol, ts64, ev.direction)

        if ev.source_type == "ai":
            wr, n = trail_wr(trail_idx, ev.source, ts64)
            conf = float(ev.confidence) if pd.notna(ev.confidence) else 0.5
        else:
            wr, n = 0.5, 0
            conf = CONV_CONFIDENCE_MAPPING.get(ev.source, 0.5)

        src_entry = float(ev.entry) if pd.notna(ev.entry) and ev.entry else 0.0
        drift = ((entry_close - src_entry) / src_entry * 100.0) if src_entry > 0 else 0.0
        drift = max(-50.0, min(50.0, drift))

        feats = build_feature_row(
            market_row, entry_close, regime_row, regime_age, swarm,
            {
                "name": ev.source, "type": ev.source_type, "conf": conf,
                "trail_wr_30d": wr, "trail_n_30d": n,
                "entry_drift_pct": drift, "direction": ev.direction,
            },
        )

        out_fh.write(json.dumps({
            "event_id": int(ev.id), "source_type": ev.source_type, "source": ev.source,
            "symbol": symbol, "ts": pd.Timestamp(ev.ts).isoformat(), "direction": ev.direction,
            "weight": float(ev.weight),
            "entry": entry1, "sl": sl, "targets": targets,
            "label": label, "net_pnl_pct": res.get("net_pnl_pct"),
            "exit_reason": res.get("exit_reason"), "risk_pct": res.get("risk_pct"),
            "features": feats,
        }) + "\n")
        stats["written"] += 1
        stats["wins"] += 1 if label == 1 else 0
        stats["open_end"] += 1 if label is None else 0


# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=SINCE_DEFAULT)
    ap.add_argument("--out", default=os.path.join(REPLAY_DIR, "aim2_events.jsonl"))
    ap.add_argument("--limit-symbols", type=int, default=0)
    ap.add_argument("--skip-entry-hour", action="store_true",
                    help="Conservative labels: do not replay the signal-hour candle (lookahead probe)")
    args = ap.parse_args()

    set_low_priority()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    t0 = time.time()

    conn = get_db_connection()
    log("Loading events…")
    ev = load_events(conn, args.since)
    n_ai = int((ev["source_type"] == "ai").sum())
    n_conv = int((ev["source_type"] == "conv").sum())
    log(f"Total events: {len(ev)} (ai={n_ai}, conv={n_conv}); "
        f"sampled for training: {int(ev['sampled'].sum())}")

    swarm_idx = build_swarm_index(ev)          # FULL stream (before sampling)
    r_ts, r_rows = load_regime(conn)
    trail_idx = load_trail_index(conn, args.since)
    log(f"Context ready: {len(swarm_idx)} symbols in swarm index, "
        f"{len(r_rows)} regime rows, {len(trail_idx)} models with trailing WR")

    train_ev = ev[ev["sampled"]]
    symbols = list(train_ev["symbol"].drop_duplicates())
    if args.limit_symbols:
        symbols = symbols[: args.limit_symbols]

    stats = {k: 0 for k in ("written", "wins", "open_end", "no_candles", "no_window",
                            "stale_join", "geometry_fail", "bad_close")}
    with open(args.out, "w", encoding="utf-8") as fh:
        for i, sym in enumerate(symbols, 1):
            sym_events = train_ev[train_ev["symbol"] == sym]
            process_symbol(conn, sym, sym_events, swarm_idx, r_ts, r_rows, trail_idx, fh, stats,
                           skip_entry_hour=args.skip_entry_hour)
            if i % 25 == 0 or i == len(symbols):
                closed = stats["written"] - stats["open_end"]
                wr = stats["wins"] / closed * 100 if closed else 0.0
                log(f"{i}/{len(symbols)} symbols | written {stats['written']} "
                    f"(WR closed: {wr:.1f}%) | skips: {stats['no_candles']} no candles, "
                    f"{stats['stale_join']} stale, {stats['geometry_fail']} geometry, "
                    f"{stats['no_window']} window | {time.time() - t0:.0f}s")
    conn.close()

    log(f"DONE -> {args.out}")
    log(json.dumps(stats))


if __name__ == "__main__":
    main()
