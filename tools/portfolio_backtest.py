# tools/portfolio_backtest.py — how many trades, and at what size.
"""Walk-forward portfolio simulation of a curated Cornix channel.

T-2026-KYT-9050-105, follow-up to T-104 (PR #274). T-104 measured per-leg edge in
percentage points per trade and deliberately stopped there, because pp/trade does
not answer either of Michi's questions. The bots' book reports +0.69 pp/trade for
AIM2 where the Cornix run implies +0.054 — a factor of 13 that lives in ladder
partials, fills and fees. Sizing and trade count only fall out of a simulation
that holds a balance, consumes margin, and lets positions block each other.

What this adds over the T-104 replay
------------------------------------
**Exit timestamps.** An expectancy table needs only the outcome; a portfolio needs
to know *when* a position gives its margin back, because that is what decides
whether the next signal can be taken at all. Every record here carries the exit
instant, and admission is evaluated against the book as it stood at that moment.

Design decisions, deliberately narrow
-------------------------------------
* **Geometry is pre-registered, not optimised.** LONG runs TP 4 % / SL 5 %, SHORT
  runs TP 3 % / SL 2 %, both taken from T-104's finding that the direction verdict
  flips with geometry (positive legs compose a book 95 % LONG at TP4/SL5 and 76 %
  SHORT at TP3/SL2). Letting the walk-forward tune geometry as well would add a
  second search dimension on top of leg selection, and the sample is eight weeks.
* **The walk-forward chooses legs only** — which (model, direction) pairs ran
  positive on the trailing window, applied frozen to the next. Plus one binary
  variant: OI gate on the short side, on or off.
* **Fixed-USD sizing.** T-104 established that a percentage of the *available*
  balance saturates (mean size 1.07 % at a 5 % setting) and collapses the
  effective sample from 299 to 131. A fixed amount is the only rule that
  allocates uniformly, so it is the only one swept here.

Why walk-forward and not one split
----------------------------------
Two measured non-stationarities forbid a single chronological cut: fleet signal
volume grew **6x** over the window (2,319/week mid-June to 15,769/week end of
July), and the ``oi_5m`` collector degraded from a 5.0 to a 10.0 min median
cadence on 2026-07-13. A first-half/second-half split would compare two different
fleets measured two different ways.

Known limitations (stated, not buried)
--------------------------------------
* Entries fill at the signal price. Cornix posts a limit at CMP and a move away
  means no fill at all; Bot 40 measured 18 of 101 open mirrors more than 1 % from
  market over a 15-minute window. This simulation is therefore optimistic on fills.
* Leverage is a constant (default 20x, the published median) — the export does not
  carry per-signal leverage.
* The TP ladder is modelled as two tranches, 50/50, with the stop to breakeven
  after TP1. T-104 measured the third rung as value-destroying (-0.739 per weight
  unit), so it is not modelled.

DB-free: consumes the ``.npz`` produced by ``tools/leg_composition_replay.py
export``. Nothing here touches the live database or the fleet.

Usage
-----
    python tools/portfolio_backtest.py --in reports/t105_raw.npz \\
        --capital 800 --sizes 1,2,4,8,16 --out reports/portfolio.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.leg_composition_replay import outcome, replay_signal  # noqa: E402

UTC = timezone.utc

# Pre-registered geometry variants — compared side by side, never fitted per
# window. T-104 found the direction verdict flips with geometry (positive legs
# compose a book 95 % LONG at TP4/SL5 and 76 % SHORT at TP3/SL2); `t104` encodes
# that asymmetry, and `inverted` is its falsification control. If `inverted`
# performs as well, the asymmetry was a grid artifact and not a property of the
# tape — that is the whole reason it is in the sweep.
GEOMETRY_VARIANTS: dict[str, dict[str, tuple[float, float]]] = {
    "t104": {"LONG": (4.0, 5.0), "SHORT": (3.0, 2.0)},
    "symmetric_wide": {"LONG": (4.0, 5.0), "SHORT": (4.0, 5.0)},
    "symmetric_tight": {"LONG": (3.0, 2.0), "SHORT": (3.0, 2.0)},
    "inverted": {"LONG": (3.0, 2.0), "SHORT": (4.0, 5.0)},
}
LADDER = (0.5, 0.5)  # TP1 / TP2 tranches; TP3 measured as value-destroying
FEE_PCT = 0.09  # taker round trip on notional
DEFAULT_LEVERAGE = 20.0
# Direction overhang. NOT from core/trailing_roster (that module only holds
# SLOT_CAP) — the live value is `40_trailing_close_bot.EXPOSURE_CAP`, which reads
# the env override TRAILING_BOT_EXPOSURE_CAP and is therefore operator-settable
# rather than a fixed constant.
EXPOSURE_CAP = 50
# Equity-curve points written per run — see "equity_curve" in the result dict.
EQUITY_POINTS = 500
# Cornix caps a channel at 500 simultaneously open trades; core/trailing_roster
# mirrors it as SLOT_CAP so the bot decides which trades get dropped instead of
# letting Cornix decide by arrival time. The first version of this simulation
# omitted it and happily held 800 positions at 1 USD — above a ceiling that
# exists in production. A backtest that ignores the venue's own limit is not
# simulating the venue.
SLOT_CAP = 500
TRAIN_WEEKS = 3  # trailing window the leg selection is fitted on
MIN_N_TRAIN = 25  # a leg needs this many trailing trades to be selectable


def precompute(z, geometry: dict[str, tuple[float, float]], oi_short_threshold: float | None, oi_feats) -> list[dict]:
    """One record per signal: entry instant, exit instant, realised pp, admission keys.

    The ladder is resolved here rather than in the event loop so the simulation
    stays a pure accounting exercise over immutable records.
    """
    meta = json.loads(str(z["meta"][0]))
    horizon_s = int(meta["horizon_h"] * 3600)
    symbols, models = list(z["symbols"]), list(z["models"])
    c_sym, c_ts, c_high, c_low, c_close = z["c_sym"], z["c_ts"], z["c_high"], z["c_low"], z["c_close"]
    s_sym, s_mod, s_long, s_ts, s_entry = z["s_sym"], z["s_mod"], z["s_long"], z["s_ts"], z["s_entry"]

    starts = np.searchsorted(c_sym, np.arange(len(symbols)), side="left")
    ends = np.searchsorted(c_sym, np.arange(len(symbols)), side="right")

    recs = []
    for k in range(len(s_ts)):
        a, b = starts[s_sym[k]], ends[s_sym[k]]
        if a == b:
            continue
        ts = c_ts[a:b]
        lo = a + int(np.searchsorted(ts, s_ts[k], side="right"))
        hi = a + int(np.searchsorted(ts, s_ts[k] + horizon_s, side="right"))
        if hi <= lo:
            continue
        direction = "LONG" if s_long[k] else "SHORT"
        tp, sl = geometry[direction]
        i_tp, i_sl, mark, _, _ = replay_signal(
            c_high[lo:hi], c_low[lo:hi], c_close[lo:hi], float(s_entry[k]), bool(s_long[k])
        )
        # second rung: the next grid level above tp, if the export carries one
        tp2 = next((d for d in sorted(float(x) for x in i_tp) if d > tp), None)

        res1 = outcome(i_tp[str(tp)], i_sl[str(sl)])
        step_exit = _exit_step(i_tp[str(tp)], i_sl[str(sl)], hi - lo)
        pnl = 0.0
        if res1 == "TP":
            pnl += LADDER[0] * tp
            if tp2 is not None:
                res2 = outcome(i_tp[str(tp2)], i_sl[str(sl)])
                # stop travels to breakeven once the first rung is taken
                pnl += LADDER[1] * (tp2 if res2 == "TP" else 0.0)
                # When the runner is credited breakeven (res2 != "TP") the position
                # closed when price came BACK to entry — not at the horizon. The
                # previous `_exit_step(i_tp[tp2], None, hi - lo)` returned n-1 in
                # that case, so the two halves of one trade disagreed: PnL said
                # "stopped at breakeven", occupancy said "held 72h". That inflated
                # hold time for the modal winner under tight geometry, i.e. exactly
                # against the "tight frees slots faster" mechanism being measured.
                step_exit = max(
                    step_exit,
                    _breakeven_step(
                        c_high[lo:hi],
                        c_low[lo:hi],
                        float(s_entry[k]),
                        bool(s_long[k]),
                        i_tp[str(tp)],
                        i_tp[str(tp2)],
                        hi - lo,
                    ),
                )
            else:
                pnl += LADDER[1] * tp
        elif res1 == "SL":
            pnl -= sl
        else:
            pnl += mark
        pnl -= FEE_PCT

        recs.append(
            {
                "model": models[s_mod[k]],
                "direction": direction,
                "open_ts": int(s_ts[k]),
                # `if step_exit` was a falsy-zero bug: `_exit_step` legitimately
                # returns 0 when TP or SL is touched on the FIRST closed candle
                # after the signal, and that 0 was read as "never exited", pinning
                # the trade to the full horizon. A five-minute round trip was booked
                # as 72 h of slot occupancy — an 864x overstatement, and worse than
                # a uniform bias because tighter geometries produce more step-0
                # touches, so it fell unevenly across the variants being compared.
                # `_exit_step` never returns None (it falls back to n-1), so the
                # index is always valid and needs no fallback at all.
                "exit_ts": int(ts[min(len(ts) - 1, (lo - a) + step_exit)]),
                "pnl_pct": pnl,
                "symbol": symbols[s_sym[k]],
                "oi": float(oi_feats["oi_chg_4h"][k]) if oi_feats is not None else np.nan,
            }
        )
    recs.sort(key=lambda r: r["open_ts"])
    if oi_short_threshold is not None:
        kept = [r for r in recs if r["direction"] == "LONG" or (r["oi"] == r["oi"] and r["oi"] <= oi_short_threshold)]
        print(f"  OI gate at {oi_short_threshold:+.2f}% keeps {len(kept):,} of {len(recs):,} signals")
        return kept
    return recs


def _exit_step(i_tp: int | None, i_sl: int | None, n: int) -> int:
    """Step index at which the position is closed; horizon end when neither fires."""
    cands = [i for i in (i_tp, i_sl) if i is not None]
    return min(cands) if cands else n - 1


def _breakeven_step(
    high: np.ndarray,
    low: np.ndarray,
    entry: float,
    is_long: bool,
    i_tp1: int | None,
    i_tp2: int | None,
    n: int,
) -> int:
    """Step at which the runner leaves after TP1 took the first half.

    If the second rung fires, that is the exit. Otherwise the stop sits at
    breakeven, so the runner leaves the first time price trades back through the
    entry after TP1 — not at the horizon end, which is what the previous
    `_exit_step(i_tp2, None, n)` returned and what made the PnL and the occupancy
    of the same trade disagree.
    """
    if i_tp2 is not None:
        return int(i_tp2)
    if i_tp1 is None:
        return n - 1
    after = slice(int(i_tp1) + 1, n)
    back = (low[after] <= entry) if is_long else (high[after] >= entry)
    hits = np.flatnonzero(back)
    return int(i_tp1) + 1 + int(hits[0]) if hits.size else n - 1


def select_legs(recs: list[dict], t0: int, t1: int) -> set[tuple[str, str]]:
    """Legs with positive mean pp, RESOLVED inside [t0, t1), and enough trades.

    Membership is on `exit_ts`, not `open_ts`. Filtering on entry while scoring
    with the fully realised `pnl_pct` is a look-ahead: at each weekly refit the
    last `horizon_s` (72 h) of the trailing window had not resolved yet, so ~43 %
    of each newly added week was selected on outcomes that had not happened. A leg
    that only turns positive in its final unresolved days was admitted a week
    early. This is the same defect class that inflated a prior study here 8x by
    checking arming over a whole lifetime instead of up to the deadline.

    The cost is that the effective training window is shorter than `t1 - t0` by
    one horizon — which is the real information set, not a loss.
    """
    agg: dict[tuple[str, str], list[float]] = defaultdict(list)
    for r in recs:
        if t0 <= r["open_ts"] and r["exit_ts"] < t1:
            agg[(r["model"], r["direction"])].append(r["pnl_pct"])
    return {k for k, v in agg.items() if len(v) >= MIN_N_TRAIN and float(np.mean(v)) > 0.0}


def simulate(recs: list[dict], capital: float, fixed_usd: float, leverage: float) -> dict:
    """Event-driven walk-forward run. Returns equity curve and risk statistics."""
    if not recs:
        return {}
    t_start, t_end = recs[0]["open_ts"], max(r["exit_ts"] for r in recs)
    week = 7 * 24 * 3600
    train_s = TRAIN_WEEKS * week

    balance = capital
    free = capital
    open_pos: list[tuple[int, dict]] = []  # (exit_ts, record)
    n_long = n_short = 0
    taken = rejected_margin = rejected_leg = rejected_cap = rejected_slots = 0
    peak_occ = 0
    equity: list[tuple[int, float]] = []
    daily: dict[str, float] = defaultdict(float)
    selection: set[tuple[str, str]] = set()
    next_refit = t_start + train_s

    events = sorted(recs, key=lambda r: r["open_ts"])
    ei = 0
    while ei < len(events) or open_pos:
        next_open = events[ei]["open_ts"] if ei < len(events) else None
        next_close = min((e for e, _ in open_pos), default=None)
        if next_open is None or (next_close is not None and next_close <= next_open):
            # close first: margin must be back before the next admission decision
            idx = min(range(len(open_pos)), key=lambda i: open_pos[i][0])
            _, rec = open_pos.pop(idx)
            notional = fixed_usd * leverage
            pnl = notional * rec["pnl_pct"] / 100.0
            balance += pnl
            free += fixed_usd
            n_long -= rec["direction"] == "LONG"
            n_short -= rec["direction"] == "SHORT"
            day = datetime.fromtimestamp(rec["exit_ts"], UTC).date().isoformat()
            daily[day] += pnl
            equity.append((rec["exit_ts"], balance))
            continue

        r = events[ei]
        ei += 1
        if r["open_ts"] >= next_refit:
            selection = select_legs(recs, r["open_ts"] - train_s, r["open_ts"])
            next_refit = r["open_ts"] + week
        if not selection or (r["model"], r["direction"]) not in selection:
            rejected_leg += 1
            continue
        if len(open_pos) >= SLOT_CAP:
            rejected_slots += 1
            continue
        overhang = (n_long - n_short) if r["direction"] == "LONG" else (n_short - n_long)
        if overhang >= EXPOSURE_CAP:
            rejected_cap += 1
            continue
        if free < fixed_usd:
            rejected_margin += 1
            continue
        free -= fixed_usd
        open_pos.append((r["exit_ts"], r))
        n_long += r["direction"] == "LONG"
        n_short += r["direction"] == "SHORT"
        taken += 1
        peak_occ = max(peak_occ, len(open_pos))

    # Two defects lived in these three lines and both understated risk.
    #
    # (a) The curve started at the balance AFTER the first close, so the initial
    #     capital was never a peak and the decline from capital to the first trough
    #     was invisible. A run losing 50 % on its only trade reported max_dd 0.0.
    # (b) `sorted(equity)` sorts (ts, balance) TUPLES, so exits sharing a timestamp
    #     — routine on a 5m grid with up to 500 open positions — were reordered
    #     ascending by balance, smoothing intra-instant dips away. A -40 %/+35 %
    #     pair closing on the same candle reported zero drawdown. `equity` is
    #     already appended in chronological processing order, so the sort was both
    #     unnecessary and harmful.
    #
    # Drawdown is the number an operator would size on, so a floor is not a
    # conservative approximation here — it is the wrong direction.
    curve = [capital] + [b for _, b in equity]
    peak = np.maximum.accumulate(curve)
    max_dd = float(((np.array(curve) - peak) / peak).min() * 100)
    days = sorted(daily.items())
    n_days = max(1, (t_end - t_start) // 86400)
    return {
        "capital": capital,
        "fixed_usd": fixed_usd,
        "leverage": leverage,
        "notional_per_trade_pct": round(fixed_usd * leverage / capital * 100, 2),
        "trades_taken": taken,
        "trades_per_day": round(taken / n_days, 1),
        "rejected": {
            "leg": rejected_leg,
            "exposure_cap": rejected_cap,
            "slot_cap": rejected_slots,
            "margin": rejected_margin,
        },
        "peak_occupancy": peak_occ,
        "peak_margin_util_pct": round(peak_occ * fixed_usd / capital * 100, 1),
        # which ceiling actually bound — reporting the return without this invites
        # reading a sample-selection artifact as a sizing result
        # "none" first: with all three counters at zero the chain below reported
        # "exposure_cap", i.e. it named a ceiling that never fired. This field
        # exists precisely so a return cannot be read without its binding
        # constraint — asserting one that did not bind is the misreading it was
        # built to prevent, and it is what made the "the slot is the scarce
        # resource" claim look supported when slot_cap rejections were 0 in every
        # committed run.
        "binding_constraint": (
            "none"
            if max(rejected_slots, rejected_margin, rejected_cap) == 0
            else "slot_cap"
            if rejected_slots > max(rejected_margin, rejected_cap)
            else "margin"
            if rejected_margin > rejected_cap
            else "exposure_cap"
        ),
        "peak_notional_x_capital": round(peak_occ * fixed_usd * leverage / capital, 1),
        "final_balance": round(balance, 2),
        "return_pct": round((balance / capital - 1) * 100, 2),
        "max_drawdown_pct": round(max_dd, 2),
        # The equity curve is the task's first-named deliverable. It was built here
        # and then discarded, while the module docstring still advertised it.
        # Down-sampled to at most EQUITY_POINTS points so a 9k-trade run does not
        # put a 9k-element array into every cell of a sweep — the shape is what a
        # reader needs, and max_drawdown_pct above carries the exact extremum.
        "equity_curve": [
            [int(ts), round(bal, 2)]
            for ts, bal in ([(t_start, capital)] + equity)[:: max(1, len(equity) // EQUITY_POINTS)]
        ],
        "worst_day": [days and min(days, key=lambda kv: kv[1])[0], round(min((v for _, v in days), default=0.0), 2)],
        "best_day": [days and max(days, key=lambda kv: kv[1])[0], round(max((v for _, v in days), default=0.0), 2)],
        "losing_days": sum(1 for _, v in days if v < 0),
        "total_days": len(days),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Walk-forward portfolio backtest (T-2026-KYT-9050-105)")
    ap.add_argument("--in", dest="infile", default="reports/t105_raw.npz")
    ap.add_argument("--oi", default=None, help="optional OI feature npz; enables the short gate")
    ap.add_argument("--oi-threshold", type=float, default=-1.2, help="short gate: keep oi_chg_4h <= this")
    ap.add_argument("--capital", type=float, default=800.0)
    ap.add_argument("--sizes", default="1,2,4,8,16", help="fixed USD amounts to sweep")
    ap.add_argument("--leverage", type=float, default=DEFAULT_LEVERAGE)
    ap.add_argument(
        "--geometries",
        default="t104,symmetric_wide,symmetric_tight,inverted",
        help=f"comma-separated, from {sorted(GEOMETRY_VARIANTS)}; `inverted` is the falsification control",
    )
    ap.add_argument("--out", default="reports/portfolio_backtest.json")
    args = ap.parse_args()

    z = np.load(args.infile, allow_pickle=True)
    oi_feats = np.load(args.oi) if args.oi else None
    print(f"Loaded {len(z['s_ts']):,} signals from {args.infile}")

    gates: list[tuple[str, float | None]] = [("no_gate", None)]
    if oi_feats is not None:
        gates.append(("oi_gate", args.oi_threshold))
    geom_names = [g.strip() for g in args.geometries.split(",")]

    results = {}
    for geom_name in geom_names:
        geometry = GEOMETRY_VARIANTS[geom_name]
        for gate_label, threshold in gates:
            print(
                f"\n=== geometry {geom_name} "
                f"(L {geometry['LONG'][0]:.0f}/{geometry['LONG'][1]:.0f}, "
                f"S {geometry['SHORT'][0]:.0f}/{geometry['SHORT'][1]:.0f}) · {gate_label} ==="
            )
            recs = precompute(z, geometry, threshold, oi_feats)
            for size in (float(s) for s in args.sizes.split(",")):
                stats = simulate(recs, args.capital, size, args.leverage)
                stats["geometry"] = geom_name
                stats["gate"] = gate_label
                results[f"{geom_name}|{gate_label}|{size}"] = stats
                print(
                    f"  {size:>5.1f} USD  trades {stats['trades_taken']:>6} ({stats['trades_per_day']:>5}/d)  "
                    f"peak occ {stats['peak_occupancy']:>4} ({stats['peak_margin_util_pct']:>5}% margin)  "
                    f"return {stats['return_pct']:>+8.2f}%  maxDD {stats['max_drawdown_pct']:>7.2f}%  "
                    f"worst day {stats['worst_day'][1]:>+8.2f}  bind {stats['binding_constraint']}"
                )

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "task": "T-2026-KYT-9050-105",
                "geometry_variants": GEOMETRY_VARIANTS,
                "ladder": LADDER,
                "fee_pct": FEE_PCT,
                "exposure_cap": EXPOSURE_CAP,
                "train_weeks": TRAIN_WEEKS,
                "results": results,
            },
            fh,
            indent=1,
        )
    print(f"\nWritten: {args.out}")


if __name__ == "__main__":
    main()
