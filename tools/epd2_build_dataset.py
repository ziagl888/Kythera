"""
tools/epd2_build_dataset.py — training events + replay labels for the
EPD2 retrain (Momentum-RIDE-ALONG, MODEL_INTENT §7).

EPD detects on 10s ticks — bar-by-bar this is not replayable. The
detector logs ARE the events though: ``pump_dump_events`` is written by Bot 10 with
exactly the live gates. Pipeline (pattern: tools/pex1_build_dataset.py,
mirrors BOT-10 semantics instead of Bot 30):

  1. Events: volume_ratio >= 5 (bot's alert gate) AND
     |price_change_60s| >= PUMP_EVENT_MIN_ABS_PCHG_60S — BOTH directions.
  2. Direction = RIDE-ALONG (Intent §7): pump (+60s move) → LONG, dump → SHORT.
  3. TZ fix + dedup 900 s per symbol (Bot 10's live alert throttle).
  4. Entry = actual price at `spike_time` from ``ticker_10s`` (T-2026-CU-9050-035).
     This used to be the estimator ``close×(1+p_chg_60s/100)``. That has been WRONG
     since ``p_chg_60s`` was normalised to a rate per 60 s: the column no longer
     carries the realised move, and without the window length it cannot be
     reconstructed from the event log (hard rule 7, trainer ==
     serving). ``ticker_10s`` holds the actually traded price at 10s
     resolution and beats any estimator — measured, 7053 of 7055 events
     from the last three days find a tick within 60 s, across all 404
     event symbols.
  5. Geometry = BOT-10 geometry as-of: get_hvn_and_sr_levels(df=95d window)
     + hvn_sr_trade_geometry + ensure_min_tp_distance (NOT smart_targets —
     Bot 10 posts HVN/SR geometry).
  6. Label: simulate_exit from event candle+2 (skip-entry-hour, aim2 precedent),
     7-day horizon.
  7. Features: the bot's 10 live features (sample_fill=1.0 as a documented
     steady-state approximation — the value is not in the event log) + the 6
     funding features (core/funding_features, operator order 2026-07-06).

Example:
  python tools/epd2_build_dataset.py                    # full build
  python tools/epd2_build_dataset.py --limit-symbols 10 # smoke test
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.pex1_build_dataset import detect_offset_h, spike_time_to_utc  # noqa: E402
from tools.research_dataset_common import (  # noqa: E402
    MIN_WINDOW,
    REPLAY_DIR,
    candles_window_start,
    df_query,
    floor_idx,
    join_is_stale,
    log,
    set_low_priority,
)

from core import config as _kcfg  # noqa: E402
from core.candles import read_candles_with_indicators  # noqa: E402
from core.database import get_db_connection  # noqa: E402
from core.funding_features import funding_features_asof, load_funding  # noqa: E402
from core.trade_utils import (  # noqa: E402
    N_PUBLISHED_TARGETS,
    ensure_min_tp_distance,
    get_hvn_and_sr_levels,
    hvn_sr_trade_geometry,
    thin_targets,
)
from tools.walkforward_sim import simulate_exit  # noqa: E402

SINCE_DEFAULT = "2026-02-25"      # start of reliable pump_dump_events history
ALERT_MIN_VOL_RATIO = 5.0         # Bot 10's alert gate (training == serving)
HORIZON_CANDLES = 7 * 24
DEDUP_SECONDS = 900               # live alert throttle per symbol (Bot 10)
TICKER_MAX_LAG_SEC = 60           # max. distance event ↔ ticker_10s tick for the entry
N_PUBLISHED = 3
LEVEL_WINDOW_H = 95 * 24          # HVN/SR window matching get_hvn_and_sr_levels live

#: 10-feature contract of Bot 10 (features_array order there).
EPD_FEATURES = [
    "vol_ratio", "p_chg_60s", "buy_pres", "volat", "sample_fill",
    "rsi", "tsi", "macd", "e9_dist", "e21_dist",
]

EPD_SQL_INDICATORS = (
    "i.rsi_14, i.tsi_fast_12_7_7, i.macd_dif_normal_12_26_9, i.ema_9, i.ema_21"
)
# Plain column names for read_candles_with_indicators (i. prefix removed).
EPD_IND_COLS = [c.strip().split(".")[-1] for c in EPD_SQL_INDICATORS.split(",") if c.strip()]


def load_events(conn, since: str, offset_h: int) -> pd.DataFrame:
    ev = df_query(
        conn,
        """
        SELECT symbol, spike_time, volume_ratio, price_change_60s, buy_pressure, volatility,
               rsi_14 AS ev_rsi, tsi AS ev_tsi, macd_dif AS ev_macd,
               ema9_distance_pct AS ev_e9, ema21_distance_pct AS ev_e21
        FROM pump_dump_events
        WHERE volume_ratio >= %s AND ABS(price_change_60s) >= %s
          AND spike_time > %s::timestamptz
        ORDER BY spike_time ASC
        """,
        (ALERT_MIN_VOL_RATIO, _kcfg.PUMP_EVENT_MIN_ABS_PCHG_60S, since),
    )
    # ~30 % of events carry EXACT event-time indicators (ev_*; an older
    # bot version wrote these along) — where present, they beat the up to 1h
    # stale 1h join fallback.
    ev["ts"] = spike_time_to_utc(ev["spike_time"], offset_h)
    ev["symbol"] = ev["symbol"].astype(str).str.upper()
    ev = ev[ev["symbol"].str.endswith("USDT")].dropna(subset=["ts"])
    ev = ev[ev["ts"] >= pd.Timestamp(since)]

    # Dedup: 900s throttle per symbol (cross-direction — like pd_state live).
    keep, last_ts = [], {}
    for row in ev.itertuples():
        prev = last_ts.get(row.symbol)
        ok = prev is None or (row.ts - prev).total_seconds() >= DEDUP_SECONDS
        keep.append(ok)
        if ok:
            last_ts[row.symbol] = row.ts
    return ev[pd.Series(keep, index=ev.index)].reset_index(drop=True)


def ticker_history_start(conn) -> pd.Timestamp | None:
    """Oldest `ticker_10s` tick as naive UTC, or None if the table is empty."""
    row = df_query(conn, "SELECT MIN(ts) AS m FROM ticker_10s")["m"].iloc[0]
    if pd.isna(row):
        return None
    return pd.Timestamp(row).tz_convert("UTC").tz_localize(None)


def load_ticker_prices(conn, symbol: str, since: str) -> tuple[np.ndarray, np.ndarray]:
    """10s ticks of a symbol as (times, prices), chronological.

    `ticker_10s.ts` is UTC-aware per TZ contract (core/ticker_10s.py) — unlike
    the naive legacy columns. Brought to naive UTC here so the comparison
    with `spike_time_to_utc(...)` runs on the same basis.
    """
    try:
        df = df_query(
            conn,
            "SELECT ts, price FROM ticker_10s WHERE symbol = %s AND ts >= %s::timestamptz ORDER BY ts ASC",
            (symbol, since),
        )
    except Exception:
        conn.rollback()
        return np.empty(0, dtype="datetime64[ns]"), np.empty(0, dtype=np.float64)
    if df.empty:
        return np.empty(0, dtype="datetime64[ns]"), np.empty(0, dtype=np.float64)
    ts = pd.to_datetime(df["ts"], utc=True).dt.tz_localize(None).values.astype("datetime64[ns]")
    return ts, df["price"].to_numpy(dtype=np.float64)


def entry_from_ticker(
    ts_arr: np.ndarray, px_arr: np.ndarray, event_ts: pd.Timestamp, max_lag: int = TICKER_MAX_LAG_SEC
) -> float | None:
    """Actually traded price at the event time, or None.

    `spike_time` is the detector's wall clock AFTER the 60s move, so the tick at
    this point is already the post-spike price — exactly what the old
    estimator was trying to approximate. No fallback to the estimator: without a tick
    the entry is unknown, and a guessed entry geometry produces a wrong
    label, not a missing one.
    """
    if ts_arr.size == 0:
        return None
    target = np.datetime64(pd.Timestamp(event_ts).to_datetime64())
    i = int(np.searchsorted(ts_arr, target))
    best_lag, best_px = None, None
    for j in (i - 1, i):
        if 0 <= j < ts_arr.size:
            lag = abs((ts_arr[j] - target) / np.timedelta64(1, "s"))
            if lag <= max_lag and (best_lag is None or lag < best_lag):
                best_lag, best_px = lag, float(px_arr[j])
    if best_px is None or best_px <= 0:
        return None
    return best_px


def load_candles_epd(conn, symbol: str, since: str) -> pd.DataFrame | None:
    """1h candles + EPD indicator columns, lookback 100d (95d level window).

    Via core.candles: CLOSED candles (include_forming=False); the caller
    trims to the last closed candle via floor_idx anyway."""
    try:
        df = read_candles_with_indicators(
            conn,
            symbol,
            "1h",
            start=candles_window_start(since, 100),
            include_forming=False,
            candle_columns=("open_time", "open", "high", "low", "close", "volume"),
            indicator_columns=EPD_IND_COLS,
        )
    except Exception:
        conn.rollback()
        return None
    if df.empty:
        return None
    df["open_time"] = pd.to_datetime(df["open_time"], utc=True).dt.tz_localize(None)
    for c in df.columns:
        if c != "open_time":
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.dropna(subset=["high", "low", "close"]).reset_index(drop=True)


def _val(df, col, i, default):
    v = df[col].iloc[i]
    try:
        fv = float(v)
        return fv if np.isfinite(fv) else default
    except (TypeError, ValueError):
        return default


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=SINCE_DEFAULT)
    ap.add_argument("--out", default=os.path.join(REPLAY_DIR, "epd2_events.jsonl"))
    ap.add_argument("--limit-symbols", type=int, default=0)
    ap.add_argument(
        "--allow-pre-ticker",
        action="store_true",
        help="Allow events before the first ticker_10s tick (they lose their entry).",
    )
    args = ap.parse_args()

    set_low_priority()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    t0 = time.time()

    conn = get_db_connection()
    offset_h = detect_offset_h(conn)
    log(f"spike_time offset: {offset_h:+d}h against UTC")

    # The entry comes from ticker_10s (step 4 in the module docstring). If `since`
    # reaches before the first tick, every event before it would be silently lost
    # to `no_ticker` — that looks like a small filter in the log but is really a
    # half-sized dataset. Better to abort loudly than shrink silently.
    tick_start = ticker_history_start(conn)
    if tick_start is None:
        log("ERROR: ticker_10s is empty — no ticks, no entry. Aborting.")
        sys.exit(2)
    if pd.Timestamp(args.since) < tick_start and not args.allow_pre_ticker:
        log(f"ERROR: --since {args.since} is before the first ticker_10s tick "
            f"({tick_start:%Y-%m-%d %H:%M} UTC). Events before that get no entry.")
        log("       Set --since to the post-restart cut (recommended), or "
            "--allow-pre-ticker if the loss is deliberate.")
        sys.exit(2)

    ev = load_events(conn, args.since, offset_h)
    n_long = int((ev["price_change_60s"] > 0).sum())
    log(f"Events after gates + dedup: {len(ev)} ({n_long} pump/LONG, "
        f"{len(ev) - n_long} dump/SHORT) across {ev['symbol'].nunique()} symbols")

    symbols = list(ev["symbol"].drop_duplicates())
    if args.limit_symbols:
        symbols = symbols[: args.limit_symbols]

    stats = {k: 0 for k in ("written", "wins", "open_end", "no_candles", "no_window",
                            "stale_join", "geometry_fail", "no_ticker")}
    with open(args.out, "w", encoding="utf-8") as fh:
        for i, sym in enumerate(symbols, 1):
            df = load_candles_epd(conn, sym, args.since)
            sym_ev = ev[ev["symbol"] == sym]
            if df is None or len(df) < MIN_WINDOW:
                stats["no_candles"] += len(sym_ev)
                continue
            fund_by_sym = load_funding(conn, [sym])
            tick_ts, tick_px = load_ticker_prices(conn, sym, args.since)
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
                p_chg = float(row.price_change_60s)
                direction = "LONG" if p_chg > 0 else "SHORT"
                is_long = direction == "LONG"
                # Entry = actual post-spike price from ticker_10s. `p_chg_60s` has
                # been a rate per 60 s since T-035, not a realised move — as a
                # markup on `close` it would simply be wrong. The sign
                # (direction) is unaffected by the normalisation.
                entry1 = entry_from_ticker(tick_ts, tick_px, row.ts)
                if entry1 is None:
                    stats["no_ticker"] += 1
                    continue
                try:
                    win = df.iloc[max(0, idx + 1 - LEVEL_WINDOW_H): idx + 1][["high", "low", "close"]]
                    supps, resis = get_hvn_and_sr_levels(None, sym, entry1, df=win)
                    entry2, sl, t_cands = hvn_sr_trade_geometry(entry1, is_long, supps, resis)
                    # T-2026-KYT-9050-147: mirror the LIVE leg (trainer == serving,
                    # hard rule 7) — thin to the published count, then backstop,
                    # then cap: identical to 10_pump_dump_detector's EPD2 path.
                    targets = ensure_min_tp_distance(
                        thin_targets(t_cands[:20], entry1, is_long, keep=N_PUBLISHED_TARGETS),
                        entry1,
                        is_long,
                        min_pct=0.05,
                    )[:N_PUBLISHED_TARGETS]
                    if not targets or sl <= 0 or entry1 <= 0:
                        raise ValueError("degenerate geometry")
                    end = min(idx + 2 + HORIZON_CANDLES, len(times))
                    res = simulate_exit(
                        times[:end], highs[:end], lows[:end], closes[:end],
                        start_idx=idx + 2, direction=direction, entry=entry1, sl=sl,
                        targets=targets, n_published=min(N_PUBLISHED, len(targets)),
                    )
                    def _ev(v, fallback):
                        try:
                            fv = float(v)
                            return fv if np.isfinite(fv) else fallback
                        except (TypeError, ValueError):
                            return fallback

                    ema9 = _val(df, "ema_9", idx, entry1)
                    ema21 = _val(df, "ema_21", idx, entry1)
                    feats = {
                        "vol_ratio": float(row.volume_ratio),
                        "p_chg_60s": p_chg,
                        "buy_pres": float(row.buy_pressure),
                        "volat": float(row.volatility),
                        "sample_fill": 1.0,  # steady-state approximation (not in the event log)
                        # event-time indicators (ev_*) preferred, else 1h join as-of
                        "rsi": _ev(row.ev_rsi, _val(df, "rsi_14", idx, 50.0)),
                        "tsi": _ev(row.ev_tsi, _val(df, "tsi_fast_12_7_7", idx, 0.0)),
                        "macd": _ev(row.ev_macd, _val(df, "macd_dif_normal_12_26_9", idx, 0.0)),
                        "e9_dist": _ev(row.ev_e9, (entry1 - ema9) / ema9 * 100 if ema9 > 0 else 0.0),
                        "e21_dist": _ev(row.ev_e21, (entry1 - ema21) / ema21 * 100 if ema21 > 0 else 0.0),
                    }
                    feats.update(funding_features_asof(fund_by_sym, sym, pd.Timestamp(row.ts, tz="UTC")))
                except Exception:
                    stats["geometry_fail"] += 1
                    continue

                label = res.get("outcome_tp1")
                if res.get("exit_reason") == "open_at_end":
                    label = None  # report-13 rule: don't label open trades
                fh.write(json.dumps({
                    "symbol": sym, "ts": pd.Timestamp(row.ts).isoformat(),
                    "direction": direction, "weight": 1.0,
                    "entry": entry1, "entry2": entry2, "sl": sl, "targets": targets[:N_PUBLISHED],
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
