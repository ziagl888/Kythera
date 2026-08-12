"""T-2026-KYT-9050-141 — stage-3 trail-tightening on the frozen T-140 hazard signal.

Pre-registration: docs/T-2026-KYT-9050-141-trail-tightening-study.md (committed before
any outcome). The signal is EXACTLY the T-140 winner cell (LONG-only, theta 0.5, frozen
model) — recomputed deterministically, never re-selected. The action replays the bot's
own core.trailing_state.TrailingState on real 10s tick paths.

Subcommands:

    python -m tools.trailing_tighten_study signal --data <dir>
        Rebuilds the frozen T-140 model deterministically, scores every trade and
        persists the first firing instant per trade (signal.pkl). Offline.

    python -m tools.trailing_tighten_study pullticks --data <dir>
        Pulls the 10s tick path [filled_at, closed_at] for every LONG trade whose
        signal fires (per-trade bounded range queries). Read-only.

    python -m tools.trailing_tighten_study replay --data <dir> [--json <file>]
        Calibration gate first (baseline trail must reproduce booked TRAIL exits),
        then the A1-A3 overlays, FIT selection, one holdout look.
"""

from __future__ import annotations

import argparse
import json
import os

import numpy as np
import pandas as pd

from core.trailing_roster import ACTIVATION_PCT, RETRACE_FRAC
from core.trailing_state import TrailingState
from tools.trailing_exit_gate_study import (
    FIT_CUTOFF_UTC,
    _mark_lookup,
    exit_mix,
    paired_stats,
    tape_down_series,
)
from tools.trailing_hazard_gate_study import (
    FEATURE_NAMES,
    EmptyVolSeries,
    VolSeries,
    build_instant_features,
    first_fire_index,
    predict_proba,
    train_logistic,
)

# ── Pre-registered constants (mirror the doc; never tune here) ────────────────

SIGNAL_THETA = 0.5  # the frozen T-140 winner cell: LONG-only / theta 0.5
VARIANTS = {
    "A1-retrace-half": {"retrace_frac": RETRACE_FRAC * 0.5, "activation": ACTIVATION_PCT},
    "A2-activation-1pct": {"retrace_frac": RETRACE_FRAC, "activation": 1.0},
    "A3-activation-zero": {"retrace_frac": RETRACE_FRAC, "activation": 0.0},
}
CALIBRATION_TOL_PP = 0.25
CALIBRATION_MIN_SHARE = 0.80


# ── Pure logic (DB-free — covered by backtest/test_trailing_tighten_study.py) ──


def replay_trail(
    entry: float,
    is_long: bool,
    ts: np.ndarray,
    price: np.ndarray,
    switch_at: float | None,
    variant: dict | None,
) -> tuple[float, float] | None:
    """Run TrailingState over one tick path; return (close_ts, close_mark_pct) or None.

    ``ts`` is epoch seconds (float, ascending), ``switch_at`` the epoch second of the
    signal instant. From the first tick at/after ``switch_at`` the live parameters are
    replaced by the variant's — the peak survives the switch (the trade keeps its
    history; only the decision rule tightens).
    """
    st = TrailingState(entry, is_long, RETRACE_FRAC, ACTIVATION_PCT)
    switched = variant is None
    for i in range(len(ts)):
        if not switched and switch_at is not None and ts[i] >= switch_at:
            st.retrace_frac = variant["retrace_frac"]
            st.activation = variant["activation"]
            switched = True
        should_close, mark, _ = st.update(float(price[i]))
        if should_close:
            return float(ts[i]), float(mark)
    return None


def overlay_outcome(
    trail_close: tuple[float, float] | None,
    actual_close_ts: float,
    actual_pct: float,
) -> tuple[float, str]:
    """Earlier-of rule: tightened-trail close before the actual close wins, else booked."""
    if trail_close is not None and trail_close[0] < actual_close_ts:
        return trail_close[1], "TIGHTENED_TRAIL"
    return actual_pct, "UNCHANGED"


# ── Signal (offline, deterministic rebuild of the frozen T-140 model) ─────────


def cmd_signal(data_dir: str) -> None:
    positions = pd.read_pickle(os.path.join(data_dir, "positions.pkl"))
    btc = pd.read_pickle(os.path.join(data_dir, "btc_1h.pkl"))
    closes = pd.read_pickle(os.path.join(data_dir, "symbol_1h_closes.pkl"))
    ticks = pd.read_pickle(os.path.join(data_dir, "hourly_first_ticks.pkl"))
    closes_5m = pd.read_pickle(os.path.join(data_dir, "symbol_5m_closes.pkl"))
    for col in ("filled_at", "closed_at"):
        positions[col] = pd.to_datetime(positions[col], utc=True)
    btc["open_time"] = pd.to_datetime(btc["open_time"], utc=True)
    closes["open_time"] = pd.to_datetime(closes["open_time"], utc=True)
    ticks["hour"] = pd.to_datetime(ticks["hour"], utc=True)
    closes_5m["open_time"] = pd.to_datetime(closes_5m["open_time"], utc=True)

    tape = tape_down_series(btc)
    mark_lookups = {sym: _mark_lookup(sym, ticks, closes) for sym in positions["symbol"].unique()}
    vol_lookups: dict[str, VolSeries] = {}
    for sym, grp in closes_5m.groupby("symbol"):
        grp = grp.sort_values("open_time")
        vol_lookups[sym] = VolSeries(pd.DatetimeIndex(grp["open_time"]), grp["close"].to_numpy())
    empty_vol = EmptyVolSeries()

    fit_mask = (positions["book"] == "bot40") & (positions["filled_at"] < FIT_CUTOFF_UTC)

    feats = []
    for pos in positions.itertuples():
        feats.append(
            build_instant_features(
                pos.filled_at,
                pos.closed_at,
                float(pos.entry),
                pos.direction == "LONG",
                tape,
                mark_lookups.get(pos.symbol, pd.Series(dtype=float)),
                vol_lookups.get(pos.symbol, empty_vol),
            )
        )

    x_parts, y_parts = [], []
    for f, pos, in_fit in zip(feats, positions.itertuples(), fit_mask, strict=True):
        if not in_fit:
            continue
        ok = f.dropna(subset=["vol_4h"])
        if ok.empty:
            continue
        x_parts.append(ok[list(FEATURE_NAMES)].to_numpy(dtype=float))
        y_parts.append(np.full(len(ok), 1.0 if float(pos.close_mark_pct) < 0 else 0.0))
    w, mu, sd = train_logistic(np.vstack(x_parts), np.concatenate(y_parts))

    fire_at: dict[int, str | None] = {}
    n_fired = 0
    for f, pos in zip(feats, positions.itertuples(), strict=True):
        t_star = None
        if pos.direction == "LONG" and len(f):
            x = np.nan_to_num(f[list(FEATURE_NAMES)].to_numpy(dtype=float), nan=0.0)
            p = predict_proba(w, mu, sd, x)
            idx = first_fire_index(p, f["vol_4h"].to_numpy(dtype=float), SIGNAL_THETA)
            if idx is not None:
                t_star = str(f["instant"].iloc[idx])
                n_fired += 1
        fire_at[int(pos.id) * 10 + (0 if pos.book == "bot40" else 1)] = t_star
    pd.to_pickle(fire_at, os.path.join(data_dir, "signal.pkl"))
    print(f"signal: fires on {n_fired}/{len(positions)} trades -> signal.pkl")


def _signal_key(pos) -> int:
    """positions.id is per-table; disambiguate the two books in one dict key."""
    return int(pos.id) * 10 + (0 if pos.book == "bot40" else 1)


# ── Tick-path pull (live PG, read-only) ───────────────────────────────────────


def cmd_pullticks(data_dir: str) -> None:
    from core.database import get_db_connection

    positions = pd.read_pickle(os.path.join(data_dir, "positions.pkl"))
    for col in ("filled_at", "closed_at"):
        positions[col] = pd.to_datetime(positions[col], utc=True)
    fire_at = pd.read_pickle(os.path.join(data_dir, "signal.pkl"))

    conn = get_db_connection()
    paths: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    try:
        cur = conn.cursor()
        n = 0
        for pos in positions.itertuples():
            key = _signal_key(pos)
            if fire_at.get(key) is None:
                continue
            cur.execute(
                """SELECT ts, price FROM ticker_10s
                   WHERE symbol = %s AND ts >= %s AND ts <= %s ORDER BY ts""",
                (pos.symbol, pos.filled_at, pos.closed_at),
            )
            rows = cur.fetchall()
            if rows:
                ts = np.array([r[0].timestamp() for r in rows])
                price = np.array([float(r[1]) for r in rows])
                paths[key] = (ts, price)
            n += 1
            if n % 250 == 0:
                print(f"  {n} trades pulled ({sum(len(p[0]) for p in paths.values())} ticks)")
    finally:
        conn.close()
    pd.to_pickle(paths, os.path.join(data_dir, "tick_paths.pkl"))
    total = sum(len(p[0]) for p in paths.values())
    print(f"pulled tick paths for {len(paths)} fired trades ({total} ticks) -> tick_paths.pkl")


# ── Replay (offline) ──────────────────────────────────────────────────────────


def cmd_replay(data_dir: str, json_out: str | None) -> None:
    positions = pd.read_pickle(os.path.join(data_dir, "positions.pkl"))
    for col in ("filled_at", "closed_at"):
        positions[col] = pd.to_datetime(positions[col], utc=True)
    fire_at = pd.read_pickle(os.path.join(data_dir, "signal.pkl"))
    paths = pd.read_pickle(os.path.join(data_dir, "tick_paths.pkl"))

    fit_mask = ((positions["book"] == "bot40") & (positions["filled_at"] < FIT_CUTOFF_UTC)).to_numpy()

    # ── Calibration gate: baseline trail vs booked TRAIL exits ────────────────
    diffs = []
    for pos in positions.itertuples():
        key = _signal_key(pos)
        if key not in paths or pos.close_reason != "TRAIL":
            continue
        ts, price = paths[key]
        base = replay_trail(float(pos.entry), pos.direction == "LONG", ts, price, None, None)
        if base is not None:
            diffs.append(abs(base[1] - float(pos.close_mark_pct)))
        else:
            diffs.append(float("inf"))  # booked TRAIL, replay never closed — a miss
    diffs_arr = np.array(diffs)
    share_ok = float((diffs_arr <= CALIBRATION_TOL_PP).mean()) if len(diffs_arr) else float("nan")
    med = float(np.median(diffs_arr[np.isfinite(diffs_arr)])) if np.isfinite(diffs_arr).any() else float("nan")
    print(
        f"calibration: {len(diffs_arr)} booked-TRAIL trades with paths; "
        f"{share_ok:.1%} within ±{CALIBRATION_TOL_PP} pp (median |diff| {med:.3f} pp)"
    )
    if not (share_ok >= CALIBRATION_MIN_SHARE):
        print("CALIBRATION GATE FAILED — the counterfactual is NOT read (pre-registered stop).")
        if json_out:
            with open(json_out, "w", encoding="utf-8") as fh:
                json.dump({"calibration": {"share_ok": share_ok, "median_abs_pp": med, "gate": "FAILED"}}, fh)
        return

    # ── Variant overlays ──────────────────────────────────────────────────────
    per_variant: dict[str, pd.DataFrame] = {}
    for vname, vparams in VARIANTS.items():
        rows = []
        for pos in positions.itertuples():
            key = _signal_key(pos)
            t_star = fire_at.get(key)
            delta = 0.0
            cf_reason = pos.close_reason
            cf_pct = float(pos.close_mark_pct)
            covered = True
            if t_star is not None:
                if key not in paths:
                    covered = False  # fired but no ticks — excluded, counted
                else:
                    ts, price = paths[key]
                    tightened = replay_trail(
                        float(pos.entry),
                        pos.direction == "LONG",
                        ts,
                        price,
                        pd.Timestamp(t_star).timestamp(),
                        vparams,
                    )
                    cf_pct, tag = overlay_outcome(tightened, pos.closed_at.timestamp(), float(pos.close_mark_pct))
                    if tag == "TIGHTENED_TRAIL":
                        cf_reason = "TIGHTENED_TRAIL"
                        delta = cf_pct - float(pos.close_mark_pct)
            rows.append(
                {
                    "id": pos.id,
                    "book": pos.book,
                    "close_reason": pos.close_reason,
                    "actual_pct": float(pos.close_mark_pct),
                    "cf_pct": cf_pct,
                    "cf_reason": cf_reason,
                    "delta": delta,
                    "uncovered": not covered,
                }
            )
        per_variant[vname] = pd.DataFrame(rows)

    results: dict[str, dict] = {
        "calibration": {"share_ok": share_ok, "median_abs_pp": med, "gate": "PASSED"},
        "fit": {},
        "holdout": None,
    }
    for vname, df in per_variant.items():
        stats = paired_stats(df.loc[fit_mask, "delta"].to_numpy())
        results["fit"][vname] = stats
        print(f"FIT {vname}: n={stats['n']} sum={stats['sum']:+.1f} mean={stats['mean']:+.4f} t={stats['t']:+.2f}")

    winner = max(results["fit"], key=lambda k: results["fit"][k]["sum"])
    print(f"\nFIT winner: {winner} — evaluated ONCE on holdout:")
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

    killed_trail = int(((hold["cf_reason"] == "TIGHTENED_TRAIL") & (hold["close_reason"] == "TRAIL")).sum())
    tightened_n = int((dfw["cf_reason"] == "TIGHTENED_TRAIL").sum())
    uncovered = int(dfw["uncovered"].sum())
    results["holdout"]["trail_closes_touched"] = killed_trail
    print(
        f"tightened-trail closes: {tightened_n} total; booked-TRAIL trades touched in holdout: "
        f"{killed_trail} (stage 1 killed 745, stage 2 killed 497); fired-but-no-ticks: {uncovered}"
    )

    print("\nHOLDOUT exit-mix ACTUAL (booked):")
    print(exit_mix(hold, "actual_pct", "close_reason").to_string())
    print("\nHOLDOUT exit-mix COUNTERFACTUAL:")
    print(exit_mix(hold, "cf_pct", "cf_reason").to_string())

    if json_out:
        with open(json_out, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2, default=str)
        print(f"results written to {json_out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("signal", "pullticks", "replay"):
        p = sub.add_parser(name)
        p.add_argument("--data", required=True)
        if name == "replay":
            p.add_argument("--json")
    args = ap.parse_args()
    if args.cmd == "signal":
        cmd_signal(args.data)
    elif args.cmd == "pullticks":
        cmd_pullticks(args.data)
    else:
        cmd_replay(args.data, args.json)


if __name__ == "__main__":
    main()
