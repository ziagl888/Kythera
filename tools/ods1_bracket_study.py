#!/usr/bin/env python
"""tools/ods1_bracket_study.py — re-derive ODS1's bracket from its own replay.

T-2026-KYT-9050-116 (operator decision Michi 2026-08-08). ODS1 shipped with
TP1 1.0 % / TP2 2.0 % / SL 2.0 %, and its own docstring calls those three numbers
"the smallest honest translation of a drift into a bracket ... the first thing to
re-derive once this bot has live rows of its own". After ~2 days live the leg is
net negative (45 closed, Σ −120 % of stake, WR 64.4 %), so this is that re-derive.

Why a replay and not the live rows
----------------------------------
The 45 live outcomes were produced UNDER the current bracket. They carry no
information about what TP 1.5 % or SL 3 % would have done — a trade that stopped
out at −2 % might have run to +4 % under a wider stop, and the closed row cannot
say. Deriving a bracket from outcomes under a different bracket is circular. So
the rule is replayed over the OI history and every candidate bracket is scored
path-dependently on the same events.

Replay == serving
-----------------
The entry rule is not re-implemented here. ``find_candidates`` and ``_as_of`` are
loaded out of ``42_ai_ods1_bot.py`` itself (importlib — the file is digit-prefixed
and not importable by name). A second implementation would drift from the bot and
this study would measure a rule nobody trades.

What the entry price is
-----------------------
The CLOSE of the 5m bar the event falls in — i.e. a uniform ~5-minute posting delay,
not an instantaneous fill — and never the OI-implied mark. Since
T-2026-KYT-9050-115 the bot anchors its geometry on the live ticker at posting
time; the candle close is the honest replay proxy for that. Using the implied mark
would re-measure exactly the stale-anchor defect that task removed.

Honest-by-construction choices, all reported rather than assumed
---------------------------------------------------------------
* **Intra-bar ambiguity.** When one 5m bar's high reaches a target and its low
  reaches the stop, the order inside the bar is unknowable. Resolved SL-first
  (pessimistic) and the frequency is printed: if it is large, the ranking is an
  artefact of that choice and the study says so.
* **Grid constraints** are the fleet's, not free parameters: rungs at least
  ``MIN_TP_GAP_PCT`` apart (ODS1 already shipped a dead rung at 0.5 % once), and
  no rung beyond the stop.
* **Chrono split with a purge gap.** A grid over hundreds of events always has a
  best cell by noise. The fit window picks it; the held-out window is what decides
  whether it was real. Both are printed for every cell — the whole surface, not
  the argmax.
* **NO-EDGE is a valid verdict.** T-096 measured a +0.41 %/1h drift with no stop.
  It is entirely possible that no TP/SL bracket captures it and the honest answer
  is the time stop ODS1's docstring already points at (bot 40, ``TIME_STOP_H``) —
  which Cornix cannot express. This tool is allowed to conclude that.

Read-only against the live DB (hard rule 1). Every candle query carries a lower
time bound: ``candles`` is a TimescaleDB hypertable and an unbounded read scans
every chunk (measured: the plain COUNT(*) times out).

Usage:
    python tools/ods1_bracket_study.py --since 2026-06-20 --out staging_models/replay
"""

from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import sys
from bisect import bisect_left, bisect_right
from collections import defaultdict

import numpy as np

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# The Windows console is cp1252 and this report prints Ø, Δ and box rules. Without
# this a 10-minute replay dies in its own final print (measured: UnicodeEncodeError
# on Δ, after the whole surface had already been computed).
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 — a stream that cannot be reconfigured still works
        pass

from core.database import get_db_connection  # noqa: E402
from core.realized_pnl import _signed_move_pct  # noqa: E402


def load_bot():
    """The bot module itself — the rule under test must be the rule that runs."""
    spec = importlib.util.spec_from_file_location("ods1", os.path.join(REPO, "42_ai_ods1_bot.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["ods1"] = mod
    spec.loader.exec_module(mod)
    return mod


ODS1 = load_bot()

#: Candidate rungs and stops, in percent. Deliberately spanning both sides of the
#: shipped 1/2/2 so the current bracket is one cell in the surface rather than the
#: centre it is compared against.
TP1_GRID = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)
TP2_GRID = (1.5, 2.0, 2.5, 3.0, 4.0)
SL_GRID = (1.0, 1.5, 2.0, 2.5, 3.0, 4.0)

#: How long a replayed trade may stay open before it is marked out at the last
#: close. T-096 measured the drift at 1 h and 4 h; 24 h matches bot 40's TIME_STOP_H
#: so the "no bracket, use the time stop" alternative is scored on equal footing.
HORIZON_H = 24

#: Purge gap between fit and holdout. Events in [split, split + PURGE_H) are DROPPED
#: from BOTH windows, never shifted into one: such an event runs for up to HORIZON_H
#: hours, so its outcome overlaps the first holdout events. `n_purged` per cell is
#: the receipt — if n_fit + n_hold equals n_events, nothing was purged.
PURGE_H = HORIZON_H


def replay_events(conn, since: str, until: str) -> list[dict]:
    """Every instant the ODS1 rule would have fired, via the bot's own functions.

    Steps the same 5-minute grid the live bot polls on (POLL_SECONDS=300) and
    applies the bot's own cooldown semantics — first event per symbol wins inside
    COOLDOWN_H, as ``on_cooldown`` does live and as T-096 deduped.

    Deliberately NOT modelled: ``MAX_EMITS_PER_CYCLE``, ``has_open_ai_signal`` and
    the drift guard. So this is every instant the RULE fires, a superset of what the
    bot posts. Harmless here because every grid cell is scored on the same event set
    and the verdict rests on a PAIRED comparison — but it is not "what went live".
    """
    lo = dt.datetime.fromisoformat(since).replace(tzinfo=dt.timezone.utc)
    hi = dt.datetime.fromisoformat(until).replace(tzinfo=dt.timezone.utc)
    window_start = lo - dt.timedelta(seconds=ODS1.LOOKBACK_S + ODS1.STALENESS_CAP_S)

    with conn.cursor() as cur:
        cur.execute("SET statement_timeout='600s'")
        cur.execute(
            """SELECT symbol, extract(epoch FROM ts)::bigint, open_interest, oi_value_usdt
                 FROM oi_5m
                WHERE ts >= %s AND ts <= %s AND open_interest > 0 AND oi_value_usdt > 0
                ORDER BY symbol, ts""",
            (window_start, hi),
        )
        rows = cur.fetchall()

    series: dict[str, list[tuple[int, float, float]]] = {}
    for symbol, ts, oi, value in rows:
        series.setdefault(symbol, []).append((int(ts), float(oi), float(value) / float(oi)))
    print(f"  oi_5m: {len(rows):,} rows over {len(series)} symbols", file=sys.stderr)

    # Per-symbol timestamp arrays for windowing. `_as_of` scans linearly and breaks
    # on the first point past `t`, so calling it against the full 49-day series
    # would be O(polls × symbols × history) — ~5e10 operations, not runnable.
    #
    # Passing a WINDOW instead is exactly semantics-preserving, not an
    # approximation: `_as_of(rows, t)` needs the last point ≤ t and voids anything
    # older than STALENESS_CAP_S, and `find_candidates` also asks for
    # `_as_of(rows, t - LOOKBACK_S)` under the same cap. Every point either call
    # can return therefore lies inside [t - LOOKBACK_S - STALENESS_CAP_S, t], and
    # points outside it cannot change the result.
    stamps = {sym: [p[0] for p in pts] for sym, pts in series.items()}
    span = ODS1.LOOKBACK_S + ODS1.STALENESS_CAP_S

    events: list[dict] = []
    last_fire: dict[str, int] = {}
    t = int(lo.timestamp())
    end = int(hi.timestamp())
    step = ODS1.POLL_SECONDS
    n_polls = 0
    while t <= end:
        n_polls += 1
        window = {}
        for sym, pts in series.items():
            ts_list = stamps[sym]
            i = bisect_left(ts_list, t - span)
            j = bisect_right(ts_list, t)
            if j - i >= 2:
                window[sym] = pts[i:j]
        cands, _usable = ODS1.find_candidates(window, t)
        for c in cands:
            prev = last_fire.get(c["symbol"])
            if prev is not None and t - prev < ODS1.COOLDOWN_H * 3600:
                continue  # the bot's own per-symbol cooldown — first event wins
            last_fire[c["symbol"]] = t
            events.append({"ts": t, "symbol": c["symbol"], "px_chg": c["px_chg"], "oi_chg": c["oi_chg"]})
        t += step
    print(f"  {n_polls:,} simulated polls -> {len(events)} deduped events", file=sys.stderr)
    return events


def load_paths(conn, events: list[dict]) -> dict[tuple[str, int], list[tuple[int, float, float, float]]]:
    """Forward 5m bars per event: [(epoch, high, low, close), ...].

    One query per symbol, each with a lower AND upper time bound — `candles` is a
    hypertable and an unbounded read scans every chunk.
    """
    by_symbol: dict[str, list[int]] = defaultdict(list)
    for e in events:
        by_symbol[e["symbol"]].append(e["ts"])

    paths: dict[tuple[str, int], list[tuple[int, float, float, float]]] = {}
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout='600s'")
        for i, (symbol, stamps) in enumerate(sorted(by_symbol.items()), 1):
            lo = dt.datetime.fromtimestamp(min(stamps), dt.timezone.utc)
            hi = dt.datetime.fromtimestamp(max(stamps) + HORIZON_H * 3600, dt.timezone.utc)
            cur.execute(
                """SELECT extract(epoch FROM open_time)::bigint, high, low, close
                     FROM candles
                    WHERE tf='5m' AND symbol=%s AND open_time >= %s AND open_time <= %s AND is_closed
                    ORDER BY open_time""",
                (symbol, lo, hi),
            )
            bars = [(int(ts), float(h), float(lo_), float(c)) for ts, h, lo_, c in cur.fetchall()]
            if i % 50 == 0:
                print(f"    …{i}/{len(by_symbol)} symbols", file=sys.stderr)
            for ts in stamps:
                fwd = [b for b in bars if b[0] >= ts]
                paths[(symbol, ts)] = fwd[: HORIZON_H * 12 + 1]
    return paths


def simulate(path: list[tuple[int, float, float, float]], tp1: float, tp2: float, sl: float) -> tuple[float, str, bool]:
    """One SHORT trade through one bracket. Returns (move_pct, reason, ambiguous).

    ``move_pct`` is the un-levered weighted move, 50/50 across the two rungs, the
    same split Cornix applies. Entry is the close of the event bar; the walk starts
    at the NEXT bar, so the entry bar's own range cannot resolve the trade — that
    would be look-ahead inside the bar the decision was made on.
    """
    if len(path) < 2:
        return 0.0, "NO_PATH", False
    entry = path[0][3]
    if entry <= 0:
        return 0.0, "NO_PATH", False

    tp1_px = entry * (1 - tp1 / 100.0)
    tp2_px = entry * (1 - tp2 / 100.0)
    sl_px = entry * (1 + sl / 100.0)

    filled: list[float] = []
    ambiguous = False
    for _ts, high, low, _close in path[1:]:
        hit_sl = high >= sl_px
        hit_tp1 = low <= tp1_px and len(filled) == 0
        hit_tp2 = low <= tp2_px and len(filled) <= 1
        if hit_sl and (hit_tp1 or hit_tp2):
            # Both ends touched inside one 5m bar; the order is unknowable from
            # OHLC. Resolved against the trade, and counted so the surface can be
            # read with that bias in mind.
            ambiguous = True
        if hit_sl:
            rest = 2 - len(filled)
            move = sum(filled) + rest * (-sl)
            return move / 2.0, "SL", ambiguous
        if hit_tp1:
            filled.append(tp1)
        if hit_tp2 and len(filled) == 1:
            filled.append(tp2)
        if len(filled) == 2:
            return (tp1 + tp2) / 2.0, "TP2", ambiguous

    # Marked out at the last close, through the SAME function the live book uses
    # (`sign * (price - entry) / entry`). Hand-rolling it here produced
    # `(entry / last - 1)`, which puts the EXIT price in the denominator while the
    # rungs are percentages of the ENTRY — two bases in one function, and numbers
    # that could not be compared against the closed book this study exists to
    # improve. Caught by test_an_unresolved_trade_is_marked_out_at_the_last_close.
    rest_move = _signed_move_pct(-1.0, entry, path[-1][3])  # SHORT
    move = sum(filled) + (2 - len(filled)) * rest_move
    return move / 2.0, "TIME", ambiguous


def _t(xs) -> float:
    """One-sample t against zero; 0.0 when it is not defined."""
    if len(xs) < 3:
        return 0.0
    sd = float(np.std(xs, ddof=1))
    return float(np.mean(xs) / (sd / np.sqrt(len(xs)))) if sd > 0 else 0.0


def score(events, paths, split_ts: int | None):
    """Every admissible grid cell, scored on fit and holdout separately.

    Per-event holdout moves are kept per cell, keyed by event, so the caller can
    run a PAIRED comparison against the live bracket. Comparing two independent
    holdout means throws away the pairing — every cell is scored on the SAME
    events, so the per-event difference has far less variance than either mean and
    is the only test with enough power to separate cells at n≈280.
    """
    cells = []
    for tp1 in TP1_GRID:
        for tp2 in TP2_GRID:
            if tp2 - tp1 < ODS1.MIN_TP_GAP_PCT:
                continue  # a rung closer than the fleet minimum is not a rung
            for sl in SL_GRID:
                if tp2 > sl:
                    continue  # a target beyond the stop can only be reached after the stop had its chance
                fit, hold, amb, purged, reasons = [], [], 0, 0, defaultdict(int)
                hold_by_event: dict[tuple[str, int], float] = {}
                for e in events:
                    p = paths.get((e["symbol"], e["ts"]))
                    if not p:
                        continue
                    move, reason, a = simulate(p, tp1, tp2, sl)
                    if reason == "NO_PATH":
                        continue
                    amb += a
                    reasons[reason] += 1
                    if split_ts is not None and e["ts"] >= split_ts + PURGE_H * 3600:
                        hold.append(move)
                        hold_by_event[(e["symbol"], e["ts"])] = move
                    elif split_ts is not None and e["ts"] >= split_ts:
                        # THE PURGE. An event inside the gap runs for up to HORIZON_H
                        # hours, so its outcome overlaps the first holdout events —
                        # keeping it in fit is exactly the leak the gap exists to stop.
                        #
                        # The first version had no such branch: everything that was not
                        # holdout fell through to fit, gap cohort included, so PURGE_H
                        # only shifted the holdout start and purged nothing. The receipt
                        # that it never fired was arithmetic — n_fit + n_hold equalled
                        # n_events exactly. `n_purged` below is the standing receipt now,
                        # and the ASSIGNMENT is pinned by
                        # test_the_purge_gap_actually_drops_the_gap_cohort (the old test
                        # asserted only the CONSTANT, which is why this survived review
                        # into a shipped artifact).
                        purged += 1
                    else:
                        fit.append(move)
                if len(fit) < 30:
                    continue
                total = len(fit) + len(hold)
                cells.append(
                    {
                        "tp1": tp1,
                        "tp2": tp2,
                        "sl": sl,
                        "n_fit": len(fit),
                        # Non-zero is the proof the purge ran: n_fit + n_hold must be
                        # LESS than n_events. Equality means the gap purged nothing.
                        "n_purged": purged,
                        "mean_fit": float(np.mean(fit)),
                        "t_fit": _t(fit),
                        "n_hold": len(hold),
                        "mean_hold": float(np.mean(hold)) if hold else None,
                        "t_hold": _t(hold),
                        # Exit mix. A cell whose trades mostly end in TIME is not
                        # really being scored on its bracket — it is being scored on
                        # holding for HORIZON_H, which is T-096's own horizon
                        # measurement wearing a bracket's clothes.
                        "time_exit_pct": 100.0 * reasons.get("TIME", 0) / max(1, total),
                        "sl_exit_pct": 100.0 * reasons.get("SL", 0) / max(1, total),
                        "_hold_by_event": hold_by_event,
                        "ambiguous_pct": 100.0 * amb / max(1, total),
                        "reasons": dict(reasons),
                    }
                )
    return cells


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-06-20", help="5m candle coverage starts here")
    ap.add_argument("--until", default=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"))
    ap.add_argument("--split", default="2026-07-25", help="fit before, hold out after (+purge gap)")
    ap.add_argument("--out", default="staging_models/replay")
    args = ap.parse_args()

    conn = get_db_connection()
    try:
        print(f"[1/3] replaying the ODS1 rule {args.since} .. {args.until}", file=sys.stderr)
        events = replay_events(conn, args.since, args.until)
        if not events:
            print("no events — nothing to score", file=sys.stderr)
            return
        print(f"[2/3] pulling forward 5m paths for {len(events)} events", file=sys.stderr)
        paths = load_paths(conn, events)
        print("[3/3] scoring the grid", file=sys.stderr)
        split_ts = int(dt.datetime.fromisoformat(args.split).replace(tzinfo=dt.timezone.utc).timestamp())
        cells = score(events, paths, split_ts)
    finally:
        conn.close()

    if not cells:
        print("no admissible cell had enough events", file=sys.stderr)
        return

    want = (ODS1.TP_PCTS[0], ODS1.TP_PCTS[1], ODS1.SL_PCT)
    live = next((c for c in cells if (c["tp1"], c["tp2"], c["sl"]) == want), None)
    cells.sort(key=lambda c: -c["mean_fit"])

    # Paired holdout FIRST, and the artifact written BEFORE anything is printed: a
    # 10-minute replay once died in its own closing print (cp1252 vs "Δ") and took
    # the whole run's output with it. Compute, persist, then render.
    best = cells[0]
    paired_stat = None
    if live and best is not live:
        paired = [
            best["_hold_by_event"][k] - live["_hold_by_event"][k]
            for k in best["_hold_by_event"]
            if k in live["_hold_by_event"]
        ]
        if len(paired) > 2:
            paired_stat = {"n": len(paired), "mean": float(np.mean(paired)), "t": _t(paired)}
    for c in cells:
        c.pop("_hold_by_event", None)

    os.makedirs(args.out, exist_ok=True)
    dest = os.path.join(args.out, "ods1_bracket_study_t116.json")
    with open(dest, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "meta": {
                    "task": "T-2026-KYT-9050-116",
                    "since": args.since,
                    "until": args.until,
                    "split": args.split,
                    "purge_h": PURGE_H,
                    "horizon_h": HORIZON_H,
                    "n_events": len(events),
                    "rule": {
                        "px_rally_pct": ODS1.PX_RALLY_PCT,
                        "oi_drop_pct": ODS1.OI_DROP_PCT,
                        "lookback_s": ODS1.LOOKBACK_S,
                        "cooldown_h": ODS1.COOLDOWN_H,
                    },
                    "live_bracket": {"tp": list(ODS1.TP_PCTS), "sl": ODS1.SL_PCT},
                    "entry": "5m candle close at the event instant (posting-time proxy, T-115)",
                    "intra_bar": "ambiguous bars resolved SL-first (pessimistic)",
                    "paired_holdout_best_minus_live": paired_stat,
                },
                "cells": cells,
            },
            fh,
            indent=2,
        )
    print(f"written: {dest}", file=sys.stderr)

    hdr = (
        f"{'TP1':>5} {'TP2':>5} {'SL':>5} | {'n_fit':>6} {'Ø fit':>8} {'t':>6} | "
        f"{'n_hold':>6} {'Ø hold':>8} {'t':>6} | {'TIME%':>6} {'SL%':>5} {'amb%':>5}"
    )

    def line(c, mark=""):
        mh = f"{c['mean_hold']:+8.3f}" if c["mean_hold"] is not None else "     n/a"
        return (
            f"{c['tp1']:5.2f} {c['tp2']:5.2f} {c['sl']:5.2f} | {c['n_fit']:6d} {c['mean_fit']:+8.3f} "
            f"{c['t_fit']:6.2f} | {c['n_hold']:6d} {mh} {c['t_hold']:6.2f} | "
            f"{c['time_exit_pct']:6.1f} {c['sl_exit_pct']:5.1f} {c['ambiguous_pct']:5.1f}{mark}"
        )

    print(f"\n=== ODS1 bracket surface — {len(events)} events, split {args.split} (+{PURGE_H}h purge) ===")
    print(hdr)
    print("-" * len(hdr))
    for c in cells[:15]:
        print(line(c, "  <-- LIVE" if c is live else ""))
    if live and live not in cells[:15]:
        print("-" * len(hdr))
        print(line(live, "  <-- LIVE"))

    # ── the questions a ranking cannot answer ────────────────────────────────
    print("\n=== verdict checks ===")
    if best["sl"] == max(SL_GRID) or best["tp2"] == max(TP2_GRID):
        print(
            f"  [!] BOUNDARY: the best cell sits on the edge of the grid (SL {best['sl']} / "
            f"TP2 {best['tp2']}). The optimum may lie outside it - the ranking is then a "
            f"statement about the grid, not about the strategy."
        )
    if best["time_exit_pct"] > 50:
        print(
            f"  [!] NOT-A-BRACKET: {best['time_exit_pct']:.0f} % of that cell's trades end at the "
            f"{HORIZON_H}h mark-out rather than at a rung or a stop, so it is mostly scoring "
            f"HOLDING - T-096's horizon return in a bracket's clothes."
        )
    if paired_stat:
        print(
            f"  PAIRED holdout, best ({best['tp1']}/{best['tp2']}/{best['sl']}) minus live "
            f"({live['tp1']}/{live['tp2']}/{live['sl']}) on the SAME {paired_stat['n']} events: "
            f"delta={paired_stat['mean']:+.3f} pp/trade, t={paired_stat['t']:+.2f}"
        )
        print(
            "  -> "
            + (
                "the improvement survives out of sample."
                if paired_stat["t"] > 2.0
                else "NOT significant out of sample - the fit ranking does not carry over."
            )
        )


if __name__ == "__main__":
    main()
