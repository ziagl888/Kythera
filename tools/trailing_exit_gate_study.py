"""T-2026-KYT-9050-139 — stage-1 LONG×tape exit-gate counterfactual for the trailing books.

Pre-registration: docs/T-2026-KYT-9050-139-trailing-exit-gate-study.md (committed before
any outcome was computed). This tool implements exactly that design and nothing more.

Two subcommands:

    python tools/trailing_exit_gate_study.py pull --out <dir>
        One read-only snapshot pull from the live PG into local pickle files (T-120
        snapshot pattern; pickle because the analysis env has no parquet engine):
        positions of both books, BTCUSDT 1h closed candles, per-(symbol, hour) first
        ticker_10s tick inside the first 10 minutes, and per-symbol 1h closes for the
        mark fallback.

    python tools/trailing_exit_gate_study.py replay --data <dir> [--json <file>]
        Offline counterfactual replay + FIT/HOLDOUT report. No DB access.

The overlay never re-derives trail/SL behaviour: a trade runs exactly as booked until the
first evaluation instant at which the gate fires, and is otherwise untouched (paired
delta = 0). A gate that fires AT FILL exits at the entry price itself: the bot's fill
detection triggers on the mark crossing the entry, so the mark at the fill instant IS the
entry and mark_pct is exactly 0 — no ticker lookup needed for that instant.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from core.trailing_state import mark_pct

# ── Pre-registered constants (mirror the doc; never tune here) ────────────────

FIT_CUTOFF_UTC = datetime(2026, 8, 4, 0, 0, tzinfo=timezone.utc)
TD1_MOMENTUM_HOURS = 4
TD2_MEAN_CANDLES = 24
GRACE_HOURS = 1.0  # G-C
TICK_WINDOW_MIN = 10  # first tick within 10 min after the instant, else candle close
BOOKS = {"bot40": "trailing_positions", "bot44": "trailing_free_positions"}
VARIANTS = [(td, g) for td in ("TD1", "TD2") for g in ("G-A", "G-B", "G-C")]

# Earliest data the study can need: Bot 40 opened 2026-07-26; TD2 needs 24 closed 1h
# candles of history before the first evaluation instant.
PULL_START_UTC = datetime(2026, 7, 24, 0, 0, tzinfo=timezone.utc)


# ── Pure logic (DB-free — covered by backtest/test_trailing_exit_gate_study.py) ──


def tape_down_series(btc: pd.DataFrame) -> pd.DataFrame:
    """Per closed BTCUSDT 1h candle: TD1/TD2 down-flags, indexed by candle CLOSE time.

    ``btc`` needs columns ``open_time`` (tz-aware UTC, ascending) and ``close``. The flag
    at close time t applies to every evaluation instant in [t, next close) — the tape can
    only flip on a candle close (hard rule 5: closed candles only).
    """
    if not btc["open_time"].is_monotonic_increasing:
        btc = btc.sort_values("open_time").reset_index(drop=True)
    close_time = btc["open_time"] + pd.Timedelta(hours=1)
    close = btc["close"].astype(float)
    td1 = close < close.shift(TD1_MOMENTUM_HOURS)
    td2 = close < close.rolling(TD2_MEAN_CANDLES).mean()
    out = pd.DataFrame({"close_time": close_time, "TD1": td1.fillna(False), "TD2": td2.fillna(False)})
    return out.reset_index(drop=True)


def tape_state_at(tape: pd.DataFrame, instant: pd.Timestamp, col: str) -> bool:
    """Tape flag of the last candle CLOSED at or before ``instant`` (False if none)."""
    idx = tape["close_time"].searchsorted(instant, side="right") - 1
    if idx < 0:
        return False
    return bool(tape.iloc[idx][col])


@dataclass
class GateExit:
    instant: pd.Timestamp
    at_fill: bool


def first_gate_fire(
    filled_at: pd.Timestamp,
    closed_at: pd.Timestamp,
    entry: float,
    tape: pd.DataFrame,
    td_col: str,
    variant: str,
    marks_at_hours: pd.Series,
) -> GateExit | None:
    """First evaluation instant in [filled_at, closed_at) at which the gate fires.

    Evaluation instants are the fill itself plus every BTC 1h candle close inside the
    position's life. ``marks_at_hours`` maps hour-instants to the symbol's mark price
    (first tick within TICK_WINDOW_MIN, candle-close fallback already resolved by the
    caller); an instant with no mark cannot satisfy G-B/G-C (mark unknown) but can
    satisfy G-A (the exit PRICE for it is resolved later by the caller).
    """
    # Fill instant: mark == entry exactly, mark_pct == 0, time-in-trade 0.
    if tape_state_at(tape, filled_at, td_col) and variant == "G-A":
        return GateExit(instant=filled_at, at_fill=True)

    lo = tape["close_time"].searchsorted(filled_at, side="right")
    for i in range(lo, len(tape)):
        instant = tape["close_time"].iloc[i]
        if instant >= closed_at:
            break
        if not tape.iloc[i][td_col]:
            continue
        if variant == "G-A":
            return GateExit(instant=instant, at_fill=False)
        mark = marks_at_hours.get(instant)
        if mark is None or (isinstance(mark, float) and math.isnan(mark)):
            continue
        underwater = mark_pct(entry, float(mark), is_long=True) < 0
        if not underwater:
            continue
        if variant == "G-B":
            return GateExit(instant=instant, at_fill=False)
        if variant == "G-C" and (instant - filled_at) >= pd.Timedelta(hours=GRACE_HOURS):
            return GateExit(instant=instant, at_fill=False)
    return None


def paired_stats(deltas: np.ndarray) -> dict:
    """Mean, t (one-sample vs 0, zeros included) and sum of paired deltas."""
    n = len(deltas)
    if n == 0:
        return {"n": 0, "mean": 0.0, "t": 0.0, "sum": 0.0}
    mean = float(np.mean(deltas))
    sd = float(np.std(deltas, ddof=1)) if n > 1 else 0.0
    t = mean / (sd / math.sqrt(n)) if sd > 0 else 0.0
    return {"n": n, "mean": mean, "t": t, "sum": float(np.sum(deltas))}


# ── Snapshot pull (live PG, read-only) ────────────────────────────────────────


def cmd_pull(out_dir: str) -> None:
    from core.database import get_db_connection

    os.makedirs(out_dir, exist_ok=True)
    conn = get_db_connection()
    try:
        frames = []
        for book, table in BOOKS.items():
            df = pd.read_sql(
                f"""SELECT id, symbol, direction, entry, sl, filled_at, closed_at,
                           close_reason, close_mark_pct
                    FROM {table}
                    WHERE posted AND filled_at IS NOT NULL AND closed_at IS NOT NULL
                      AND close_mark_pct IS NOT NULL AND entry > 0
                      AND filled_at >= %(lo)s""",
                conn,
                params={"lo": PULL_START_UTC},
            )
            df["book"] = book
            frames.append(df)
        positions = pd.concat(frames, ignore_index=True)
        positions.to_pickle(os.path.join(out_dir, "positions.pkl"))

        symbols = sorted(positions["symbol"].unique().tolist())
        hi = datetime.now(timezone.utc)

        btc = pd.read_sql(
            """SELECT open_time, close FROM candles
               WHERE symbol='BTCUSDT' AND tf='1h' AND is_closed
                 AND open_time >= %(lo)s ORDER BY open_time""",
            conn,
            params={"lo": PULL_START_UTC - timedelta(hours=TD2_MEAN_CANDLES + 2)},
        )
        btc.to_pickle(os.path.join(out_dir, "btc_1h.pkl"))

        closes = pd.read_sql(
            """SELECT symbol, open_time, close FROM candles
               WHERE tf='1h' AND is_closed AND symbol = ANY(%(syms)s)
                 AND open_time >= %(lo)s ORDER BY symbol, open_time""",
            conn,
            params={"syms": symbols, "lo": PULL_START_UTC},
        )
        closes.to_pickle(os.path.join(out_dir, "symbol_1h_closes.pkl"))

        # Per (symbol, hour): FIRST tick within the first TICK_WINDOW_MIN minutes.
        # Chunked by 3-day windows so each hypertable scan stays bounded (T-116).
        tick_frames = []
        lo = PULL_START_UTC
        while lo < hi:
            chunk_hi = min(lo + timedelta(days=3), hi)
            tick_frames.append(
                pd.read_sql(
                    """SELECT DISTINCT ON (symbol, date_trunc('hour', ts))
                              symbol, date_trunc('hour', ts) AS hour, ts, price
                       FROM ticker_10s
                       WHERE ts >= %(lo)s AND ts < %(hi)s
                         AND symbol = ANY(%(syms)s)
                         AND extract(minute FROM ts) < %(win)s
                       ORDER BY symbol, date_trunc('hour', ts), ts""",
                    conn,
                    params={"lo": lo, "hi": chunk_hi, "syms": symbols, "win": TICK_WINDOW_MIN},
                )
            )
            lo = chunk_hi
        ticks = pd.concat(tick_frames, ignore_index=True)
        ticks.to_pickle(os.path.join(out_dir, "hourly_first_ticks.pkl"))
        print(
            f"pulled: {len(positions)} positions ({positions['book'].value_counts().to_dict()}), "
            f"{len(btc)} BTC 1h candles, {len(closes)} symbol closes, {len(ticks)} hourly ticks "
            f"-> {out_dir}"
        )
    finally:
        conn.close()


# ── Replay (offline) ──────────────────────────────────────────────────────────


def _mark_lookup(symbol: str, ticks: pd.DataFrame, closes: pd.DataFrame) -> pd.Series:
    """hour-instant -> mark price for one symbol: first tick, else 1h close fallback.

    The candle whose CLOSE is the instant is the one that OPENED an hour earlier.
    """
    t = ticks[ticks["symbol"] == symbol]
    tick_map = pd.Series(t["price"].values, index=pd.DatetimeIndex(t["hour"]))
    c = closes[closes["symbol"] == symbol]
    close_map = pd.Series(c["close"].values, index=pd.DatetimeIndex(c["open_time"]) + pd.Timedelta(hours=1))
    combined = close_map.copy()
    combined.loc[tick_map.index.intersection(combined.index)] = tick_map
    extra = tick_map.index.difference(combined.index)
    return pd.concat([combined, tick_map.loc[extra]]).sort_index()


def replay_variant(
    positions: pd.DataFrame,
    tape: pd.DataFrame,
    mark_lookups: dict[str, pd.Series],
    td_col: str,
    variant: str,
) -> pd.DataFrame:
    """Per-trade paired deltas for one (tape, gate) cell. SHORTs pass through at delta 0."""
    rows = []
    for pos in positions.itertuples():
        delta = 0.0
        gate_reason = None
        gate_pct = None
        excluded_no_mark = False
        if pos.direction == "LONG":
            marks = mark_lookups.get(pos.symbol, pd.Series(dtype=float))
            fire = first_gate_fire(pos.filled_at, pos.closed_at, float(pos.entry), tape, td_col, variant, marks)
            if fire is not None:
                if fire.at_fill:
                    gate_pct = 0.0
                else:
                    mark = marks.get(fire.instant)
                    if mark is None or (isinstance(mark, float) and math.isnan(mark)):
                        excluded_no_mark = True  # fired, but no tick and no candle close
                    else:
                        gate_pct = mark_pct(float(pos.entry), float(mark), is_long=True)
                if gate_pct is not None:
                    delta = gate_pct - float(pos.close_mark_pct)
                    gate_reason = "GATE"
        rows.append(
            {
                "id": pos.id,
                "book": pos.book,
                "symbol": pos.symbol,
                "direction": pos.direction,
                "filled_at": pos.filled_at,
                "close_reason": pos.close_reason,
                "actual_pct": float(pos.close_mark_pct),
                "gate_pct": gate_pct,
                "cf_reason": gate_reason or pos.close_reason,
                "delta": delta,
                "excluded_no_mark": excluded_no_mark,
            }
        )
    df = pd.DataFrame(rows)
    return df


def exit_mix(df: pd.DataFrame, pct_col: str, reason_col: str) -> pd.DataFrame:
    g = df.groupby(reason_col)[pct_col].agg(["count", "sum", "mean"])
    return g.sort_values("sum")


def cmd_replay(data_dir: str, json_out: str | None) -> None:
    positions = pd.read_pickle(os.path.join(data_dir, "positions.pkl"))
    btc = pd.read_pickle(os.path.join(data_dir, "btc_1h.pkl"))
    closes = pd.read_pickle(os.path.join(data_dir, "symbol_1h_closes.pkl"))
    ticks = pd.read_pickle(os.path.join(data_dir, "hourly_first_ticks.pkl"))

    for col in ("filled_at", "closed_at"):
        positions[col] = pd.to_datetime(positions[col], utc=True)
    btc["open_time"] = pd.to_datetime(btc["open_time"], utc=True)
    closes["open_time"] = pd.to_datetime(closes["open_time"], utc=True)
    ticks["hour"] = pd.to_datetime(ticks["hour"], utc=True)

    tape = tape_down_series(btc)
    mark_lookups = {sym: _mark_lookup(sym, ticks, closes) for sym in positions["symbol"].unique()}

    fit_mask = (positions["book"] == "bot40") & (positions["filled_at"] < FIT_CUTOFF_UTC)
    print(
        f"positions: {len(positions)} total, FIT {int(fit_mask.sum())}, "
        f"HOLDOUT {int((~fit_mask).sum())} "
        f"(bot40-holdout {int(((positions['book'] == 'bot40') & ~fit_mask).sum())}, "
        f"bot44 {int((positions['book'] == 'bot44').sum())})"
    )

    results: dict[str, dict] = {"fit": {}, "holdout": None}
    per_variant: dict[str, pd.DataFrame] = {}
    for td_col, variant in VARIANTS:
        key = f"{td_col}/{variant}"
        df = replay_variant(positions, tape, mark_lookups, td_col, variant)
        per_variant[key] = df
        stats = paired_stats(df.loc[fit_mask, "delta"].to_numpy())
        results["fit"][key] = stats
        print(f"FIT {key}: n={stats['n']} sum={stats['sum']:+.1f} mean={stats['mean']:+.4f} t={stats['t']:+.2f}")

    winner = max(results["fit"], key=lambda k: results["fit"][k]["sum"])
    print(f"\nFIT winner (by net sum): {winner} — evaluated ONCE on holdout:")

    dfw = per_variant[winner]
    hold = dfw.loc[~fit_mask]
    all_stats = paired_stats(hold["delta"].to_numpy())
    by_book = {book: paired_stats(hold.loc[hold["book"] == book, "delta"].to_numpy()) for book in ("bot40", "bot44")}
    results["holdout"] = {"variant": winner, "all": all_stats, "by_book": by_book}
    print(
        f"HOLDOUT all: n={all_stats['n']} sum={all_stats['sum']:+.1f} mean={all_stats['mean']:+.4f} t={all_stats['t']:+.2f}"
    )
    for book, s in by_book.items():
        print(f"  {book}: n={s['n']} sum={s['sum']:+.1f} mean={s['mean']:+.4f} t={s['t']:+.2f}")

    print("\nHOLDOUT exit-mix ACTUAL (booked):")
    print(exit_mix(hold, "actual_pct", "close_reason").to_string())
    cf = hold.copy()
    cf["cf_pct"] = np.where(cf["cf_reason"] == "GATE", cf["gate_pct"], cf["actual_pct"])
    print("\nHOLDOUT exit-mix COUNTERFACTUAL:")
    print(exit_mix(cf, "cf_pct", "cf_reason").to_string())

    fired = dfw["cf_reason"].eq("GATE")
    print(
        f"\ngate fired on {int(fired.sum())}/{len(dfw)} trades "
        f"({int((fired & ~fit_mask).sum())} in holdout); "
        f"fired-but-no-mark exclusions: {int(dfw['excluded_no_mark'].sum())}; "
        f"SHORTs untouched by construction: n={int((dfw['direction'] == 'SHORT').sum())}"
    )

    if json_out:
        with open(json_out, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2, default=str)
        print(f"results written to {json_out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_pull = sub.add_parser("pull")
    p_pull.add_argument("--out", required=True)
    p_rep = sub.add_parser("replay")
    p_rep.add_argument("--data", required=True)
    p_rep.add_argument("--json")
    args = ap.parse_args()
    if args.cmd == "pull":
        cmd_pull(args.out)
    else:
        cmd_replay(args.data, args.json)


if __name__ == "__main__":
    main()
