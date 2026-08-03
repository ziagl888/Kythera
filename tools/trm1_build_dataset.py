"""
tools/trm1_build_dataset.py — training events + labels for TRM1
"Transition-Resolution" (Report 15, p.10). Runs on the VPS (Step 2).

Events = TRANSITION checks from regime_history (raw classifications on a
5-min grid, ts = naive UTC), sampled every 30 min per episode (the checks
are highly autocorrelated — denser sampling would be pseudo-n).

Label (class contract core.research_features):
  1 = TREND_UP, 2 = TREND_DOWN, 0 = OTHER (CHOP/HIGH_VOLA) — the first
  non-TRANSITION classification after the event that is stable (>= 4 of the
  5 following checks match). No resolution within 24h → event discarded.

Additionally per event: simulated BTC trade PnL for BOTH directions
(calculate_smart_targets + simulate_exit, 14-day horizon) — the trainer
picks the threshold via the replay PnL of the respectively predicted direction.

Known, documented skew: live Bot 32 is gated on the DEBOUNCED regime
(regime_current), the events here are raw checks.

Example:
  python tools/trm1_build_dataset.py
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd

# REPO_ROOT MUST be on sys.path before the first tools/core import
# (chicken-and-egg; spec review fix 2026-07-06, pattern tools/aim2_build_dataset.py).
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
    TRM1_CLASS_DOWN,
    TRM1_CLASS_OTHER,
    TRM1_CLASS_UP,
    TRM1_WINDOW_CHECKS,
    build_trm1_row,
)
from core.trade_utils import calculate_smart_targets  # noqa: E402
from tools.walkforward_sim import simulate_exit  # noqa: E402

SINCE_DEFAULT = "2026-04-01"     # regime data only runs since ~April
TRADE_SYMBOL = "BTCUSDT"
HORIZON_CANDLES = 14 * 24
N_PUBLISHED = 3
SAMPLE_MINUTES = 30              # one event per 30 min episode
EPISODE_GAP_MIN = 15             # >15 min gap in the grid → new episode
RESOLVE_LOOKAHEAD_H = 24
STABLE_NEED = 4                  # >= 4 of the 5 following checks match → stable

CLASS_MAP = {"TREND_UP": TRM1_CLASS_UP, "TREND_DOWN": TRM1_CLASS_DOWN}


def load_history(conn, since: str) -> list[dict]:
    df = df_query(
        conn,
        """
        SELECT ts, regime, btc_return_1h, btc_return_4h, btc_atr_1h_pct,
               btc_atr_4h_pct, btcdom_return_24h, confidence_btc, confidence_alt
        FROM regime_history
        WHERE ts >= %s::timestamp - INTERVAL '1 day'
        ORDER BY ts ASC
        """,
        (since,),
    )
    df["ts"] = pd.to_datetime(df["ts"])
    df["regime"] = df["regime"].astype(str).str.upper()
    return df.to_dict("records")


def resolve_label(hist: list[dict], i: int) -> tuple[int, float] | None:
    """First stable non-TRANSITION classification after hist[i].
    Return value (class, minutes to resolution) or None (none within 24h)."""
    t0 = hist[i]["ts"]
    j = i + 1
    while j < len(hist):
        dt_min = (hist[j]["ts"] - t0).total_seconds() / 60.0
        if dt_min > RESOLVE_LOOKAHEAD_H * 60:
            return None
        reg = hist[j]["regime"]
        if reg != "TRANSITION":
            follow = [hist[k]["regime"] for k in range(j + 1, min(j + 6, len(hist)))]
            if len(follow) >= 5 and sum(1 for r in follow if r == reg) >= STABLE_NEED:
                return CLASS_MAP.get(reg, TRM1_CLASS_OTHER), dt_min
            # unstable outlier — keep searching
        j += 1
    return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default=SINCE_DEFAULT)
    ap.add_argument("--out", default=os.path.join(REPLAY_DIR, "trm1_events.jsonl"))
    args = ap.parse_args()

    set_low_priority()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    t0 = time.time()

    conn = get_db_connection()
    hist = load_history(conn, args.since)
    log(f"regime_history rows: {len(hist)}")
    if len(hist) < TRM1_WINDOW_CHECKS + 50:
        raise SystemExit("Not enough regime_history — is 26_regime_detector running?")

    btc = load_candles_ctx(conn, TRADE_SYMBOL, args.since, lookback_days=40)
    conn.close()
    if btc is None or len(btc) < MIN_WINDOW:
        raise SystemExit(f"{TRADE_SYMBOL} candles not loadable.")
    times = btc["open_time"].values.astype("datetime64[ns]")
    highs = btc["high"].to_numpy(dtype=np.float64)
    lows = btc["low"].to_numpy(dtype=np.float64)
    closes = btc["close"].to_numpy(dtype=np.float64)

    since_ts = pd.Timestamp(args.since)
    stats = {k: 0 for k in ("written", "unresolved", "no_window", "stale_join",
                            "geometry_fail", "class_up", "class_down", "class_other")}
    episode_start = None
    last_sampled = None

    with open(args.out, "w", encoding="utf-8") as fh:
        for i, row in enumerate(hist):
            if row["regime"] != "TRANSITION" or row["ts"] < since_ts:
                episode_start = None
                continue
            # episode tracking (gaps in the 5-min grid end the episode)
            if (
                episode_start is None
                or (row["ts"] - hist[i - 1]["ts"]).total_seconds() / 60.0 > EPISODE_GAP_MIN
                or hist[i - 1]["regime"] != "TRANSITION"
            ):
                episode_start = row["ts"]
                last_sampled = None
            minutes_in = (row["ts"] - episode_start).total_seconds() / 60.0
            if last_sampled is not None and (row["ts"] - last_sampled).total_seconds() / 60.0 < SAMPLE_MINUTES:
                continue
            last_sampled = row["ts"]

            if i + 1 < TRM1_WINDOW_CHECKS:
                continue
            resolved = resolve_label(hist, i)
            if resolved is None:
                stats["unresolved"] += 1
                continue
            label_class, minutes_to_res = resolved

            idx = floor_idx(times, row["ts"])
            if idx < MIN_WINDOW:
                stats["no_window"] += 1
                continue
            if join_is_stale(times, idx, row["ts"]):
                stats["stale_join"] += 1
                continue

            try:
                window = hist[i + 1 - TRM1_WINDOW_CHECKS: i + 1]
                feats = build_trm1_row(window, minutes_in)
                entry_close = float(closes[idx])
                win = btc.iloc[max(0, idx - WINDOW_CANDLES + 1): idx + 1]
                pnl = {}
                for direction in ("LONG", "SHORT"):
                    setup = calculate_smart_targets(None, TRADE_SYMBOL, direction, entry_close, df=win)
                    targets = [float(t) for t in setup["targets"][:N_PUBLISHED]]
                    if not targets:
                        raise ValueError("degenerate geometry")
                    end = min(idx + 1 + HORIZON_CANDLES, len(times))
                    res = simulate_exit(
                        times[:end], highs[:end], lows[:end], closes[:end],
                        start_idx=idx + 1, direction=direction,
                        entry=float(setup["entry1"]), sl=float(setup["sl"]),
                        targets=targets, n_published=len(targets),
                    )
                    open_end = res.get("exit_reason") == "open_at_end"
                    pnl[direction] = {
                        "net_pnl_pct": res.get("net_pnl_pct"),
                        "outcome_tp1": None if open_end else res.get("outcome_tp1"),
                    }
            except Exception:
                stats["geometry_fail"] += 1
                continue

            fh.write(json.dumps({
                "ts": pd.Timestamp(row["ts"]).isoformat(),
                "symbol": TRADE_SYMBOL, "weight": 1.0,
                "label_class": int(label_class),
                "minutes_in_transition": round(minutes_in, 1),
                "minutes_to_resolution": round(minutes_to_res, 1),
                "pnl_long": pnl["LONG"]["net_pnl_pct"],
                "pnl_short": pnl["SHORT"]["net_pnl_pct"],
                "win_long": pnl["LONG"]["outcome_tp1"],
                "win_short": pnl["SHORT"]["outcome_tp1"],
                "features": feats,
            }) + "\n")
            stats["written"] += 1
            key = {TRM1_CLASS_UP: "class_up", TRM1_CLASS_DOWN: "class_down"}.get(label_class, "class_other")
            stats[key] += 1

    log(f"DONE -> {args.out} ({time.time() - t0:.0f}s)")
    log(json.dumps(stats))


if __name__ == "__main__":
    main()
