# tools/liq_exit_replay_study.py — close bot-40 positions when liquidations run against them?
"""Counterfactual exit replay on the bot-40 trailing book: what if the bot had
CLOSED an open position the moment a liquidation cascade started running
against it? T-2026-KYT-9050-121 (Michi, 2026-08-09), follow-up of T-120.

Why this is not T-094/T-120 again: those tested ENTRY-time information (no
edge). An exit rule uses information that arrives WHILE the trade is open —
untested so far. And why it is not T-052 again: the SL-cap study only counted
each trade's own PnL delta. Michi's critique stands — a closed loser also
FREES ITS SLOT, and a free slot earns the book's baseline net-per-slot-day
(T-042 metric). This study reports both accountings.

DB-FREE: reads the T-120 DuckDB snapshot (tools/gate_snapshot_export.py) —
tables `trailing` + `liq` suffice. The counterfactual exit price is the
avg_price of the triggering liquidation event itself (a real print at trigger
time; Cornix would close at market moments later — slippage caveat documented).

Trigger definitions are the PRE-REGISTERED T-120 cascade cuts (imported, not
re-invented): >= CASCADE_MIN_N_15 against-side events within 15 min, or
>= CASCADE_MIN_N_60 within 60 min. "Against" = liquidations of the trade's own
side (SELL vs LONG, BUY vs SHORT). Only events strictly inside the position's
open interval count — a cascade that completed before entry is entry-gate
territory (T-120), not an exit signal.

Variants per cascade window (Michi's design):
  V0 close on first cascade, unconditionally
  V1 close only if the position is already >= 50% in the red LEVERED at the
     trigger (channel view ~20x => unlevered mark <= -2.5%)
  V2 same with >= 100% levered (unlevered mark <= -5.0%)
Conditions re-arm: if the first cascade hits while the mark is above the
threshold, later cascades still qualify.

The T-052 trap is measured, not assumed: results decompose by realized
close_reason, so "SL damage avoided" and "TRAIL wins truncated" are separate
numbers. MIN_LIQ_DAYS discipline as in T-120 (--smoke below 21 days).

Invocation:
  python tools/liq_exit_replay_study.py --snapshot .local/gate_snapshots/gate_snapshot_YYYYMMDD.duckdb [--smoke]
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
os.chdir(REPO_ROOT)

from tools.funding_liq_gate_study import (  # noqa: E402
    CASCADE_MIN_N_15,
    CASCADE_MIN_N_60,
    MIN_LIQ_DAYS,
    liq_coverage_days,
)
from tools.gate_snapshot import read_snapshot  # noqa: E402

LEV_VIEW = 20.0  # the levered channel view Michi sees is ~20x the unlevered marks
#: (label, unlevered mark threshold at trigger; None = unconditional V0)
VARIANTS: tuple[tuple[str, float | None], ...] = (
    ("V0 any cascade", None),
    ("V1 only if <= -50% levered (-2.5% unlev)", -2.5),
    ("V2 only if <= -100% levered (-5.0% unlev)", -5.0),
)
#: (label, min events, window minutes) — the pre-registered T-120 cuts.
CASCADES: tuple[tuple[str, int, int], ...] = (
    (f"15m/n>={CASCADE_MIN_N_15}", CASCADE_MIN_N_15, 15),
    (f"60m/n>={CASCADE_MIN_N_60}", CASCADE_MIN_N_60, 60),
)

_NS_PER_MIN = 60 * 1_000_000_000
_NS_PER_DAY = 86_400 * 1_000_000_000


# ──────────────────────────────────────────────────────────────────────────────
# pure mechanics (unit-tested)
# ──────────────────────────────────────────────────────────────────────────────


def mark_pct(direction: str, entry: float, price: float) -> float:
    """Unlevered %-mark of a position at ``price`` (close_mark_pct convention)."""
    if direction.upper() == "LONG":
        return (price / entry - 1.0) * 100.0
    return (1.0 - price / entry) * 100.0


def cascade_trigger_indices(ts_ns: np.ndarray, k: int, w_ns: int) -> np.ndarray:
    """Indices of events that COMPLETE a cascade: event i triggers when events
    i-k+1..i all lie within ``w_ns``. Every completing event is returned (the
    conditional variants may need a later, deeper trigger)."""
    if len(ts_ns) < k:
        return np.empty(0, dtype=int)
    span = ts_ns[k - 1 :] - ts_ns[: len(ts_ns) - k + 1]
    return np.nonzero(span <= w_ns)[0] + k - 1


def first_qualifying_exit(
    direction: str,
    entry: float,
    opened_ns: int,
    closed_ns: int,
    ev_ts_ns: np.ndarray,
    ev_px: np.ndarray,
    k: int,
    w_ns: int,
    min_loss_pct: float | None,
) -> tuple[int, float, float] | None:
    """First cascade trigger strictly inside (opened, closed) whose mark also
    satisfies the loss condition. Returns (trigger_ns, trigger_px, cf_mark) or
    None. ``ev_*`` are the symbol's against-side events, ascending."""
    lo = int(np.searchsorted(ev_ts_ns, opened_ns, side="right"))
    hi = int(np.searchsorted(ev_ts_ns, closed_ns, side="left"))
    ts, px = ev_ts_ns[lo:hi], ev_px[lo:hi]
    for i in cascade_trigger_indices(ts, k, w_ns):
        cf = mark_pct(direction, entry, float(px[i]))
        if min_loss_pct is None or cf <= min_loss_pct:
            return int(ts[i]), float(px[i]), cf
    return None


# ──────────────────────────────────────────────────────────────────────────────
# replay
# ──────────────────────────────────────────────────────────────────────────────


def prepare_book(trailing: pd.DataFrame) -> pd.DataFrame:
    df = trailing.copy()
    df["opened_at"] = pd.to_datetime(df["opened_at"], utc=True).astype("datetime64[ns, UTC]")
    df["closed_at"] = pd.to_datetime(df["closed_at"], utc=True).astype("datetime64[ns, UTC]")
    df = df.dropna(subset=["opened_at", "closed_at", "entry", "close_mark_pct"])
    df = df[df["entry"] > 0]
    df["opened_ns"] = df["opened_at"].values.astype("datetime64[ns]").astype("int64")
    df["closed_ns"] = df["closed_at"].values.astype("datetime64[ns]").astype("int64")
    return df


def against_events(liq: pd.DataFrame) -> dict[tuple[str, str], tuple[np.ndarray, np.ndarray]]:
    """(symbol, side) → (ts_ns ascending, avg_price). avg_price is the executed
    print of the forced order — the counterfactual exit price."""
    liq = liq.sort_values("ts")
    out: dict[tuple[str, str], tuple[np.ndarray, np.ndarray]] = {}
    for (s, sd), g in liq.groupby(["symbol", "side"], sort=False):
        ts = g["ts"].values.astype("datetime64[ns]").astype("int64")
        px = g["avg_price"].astype(float).to_numpy()
        out[(str(s), str(sd))] = (ts, px)
    return out


def replay_variant(book: pd.DataFrame, ev: dict, k: int, w_min: int, min_loss_pct: float | None) -> pd.DataFrame:
    """One (cascade cut × condition) pass over the book. Returns the triggered
    subset with counterfactual columns."""
    w_ns = w_min * _NS_PER_MIN
    rows = []
    for pos in book.itertuples(index=False):
        side = "SELL" if pos.direction.upper() == "LONG" else "BUY"
        pack = ev.get((pos.symbol, side))
        if pack is None:
            continue
        hit = first_qualifying_exit(
            pos.direction, float(pos.entry), pos.opened_ns, pos.closed_ns, pack[0], pack[1], k, w_ns, min_loss_pct
        )
        if hit is None:
            continue
        trig_ns, _trig_px, cf = hit
        rows.append(
            {
                "id": pos.id,
                "direction": pos.direction.upper(),
                "close_reason": pos.close_reason,
                "opened_ns": pos.opened_ns,
                "realized": float(pos.close_mark_pct),
                "counterfactual": cf,
                "delta": cf - float(pos.close_mark_pct),
                "freed_days": (pos.closed_ns - trig_ns) / _NS_PER_DAY,
            }
        )
    return pd.DataFrame(rows)


def per_slot_day_baseline(book: pd.DataFrame) -> float:
    """Book net per slot-day (T-042 metric). Computed on the SAME book it is
    later credited against — a documented circularity, reported, not hidden."""
    days = (book["closed_ns"] - book["opened_ns"]).sum() / _NS_PER_DAY
    return float(book["close_mark_pct"].sum() / days) if days > 0 else 0.0


def summarize_variant(trig: pd.DataFrame, book: pd.DataFrame, baseline_psd: float) -> dict:
    n_book = len(book)
    if not len(trig):
        return {"n_triggered": 0, "n_book": n_book}
    med = book["opened_ns"].median()
    halves = {}
    for name, sub in (("val", trig[trig["opened_ns"] < med]), ("test", trig[trig["opened_ns"] >= med])):
        halves[name] = {"n": int(len(sub)), "delta_sum": round(float(sub["delta"].sum()), 2)}
    by_reason = {
        str(r): {
            "n": int(len(g)),
            "realized_sum": round(float(g["realized"].sum()), 2),
            "cf_sum": round(float(g["counterfactual"].sum()), 2),
            "delta_sum": round(float(g["delta"].sum()), 2),
        }
        for r, g in trig.groupby("close_reason")
    }
    freed = float(trig["freed_days"].sum())
    delta = float(trig["delta"].sum())
    slot_credit = freed * baseline_psd
    return {
        "n_triggered": int(len(trig)),
        "n_book": n_book,
        "coverage": round(len(trig) / n_book, 4),
        "realized_sum": round(float(trig["realized"].sum()), 2),
        "counterfactual_sum": round(float(trig["counterfactual"].sum()), 2),
        "delta_sum": round(delta, 2),
        "freed_slot_days": round(freed, 1),
        "slot_credit": round(slot_credit, 2),
        "delta_incl_slot_credit": round(delta + slot_credit, 2),
        "by_close_reason": by_reason,
        "halves": halves,
        "halves_sign_agree": bool(
            np.sign(halves["val"]["delta_sum"]) == np.sign(halves["test"]["delta_sum"])
            if halves["val"]["n"] and halves["test"]["n"]
            else False
        ),
        "by_direction": {
            str(d): {"n": int(len(g)), "delta_sum": round(float(g["delta"].sum()), 2)}
            for d, g in trig.groupby("direction")
        },
    }


def run_replay(trailing: pd.DataFrame, liq: pd.DataFrame) -> dict:
    book = prepare_book(trailing)
    lo, hi = liq["ts"].min(), liq["ts"].max()
    book = book[(book["opened_at"] >= lo) & (book["closed_at"] <= hi)]
    if not len(book):
        return {"note": "no positions fully inside liq_events coverage"}
    ev = against_events(liq)
    baseline_psd = per_slot_day_baseline(book)
    out: dict = {
        "book": {
            "n": int(len(book)),
            "mark_sum": round(float(book["close_mark_pct"].sum()), 2),
            "per_slot_day_baseline": round(baseline_psd, 4),
            "span_utc": [str(book["opened_at"].min()), str(book["closed_at"].max())],
        },
        "variants": {},
    }
    for c_label, k, w_min in CASCADES:
        for v_label, thr in VARIANTS:
            trig = replay_variant(book, ev, k, w_min, thr)
            out["variants"][f"{c_label} · {v_label}"] = summarize_variant(trig, book, baseline_psd)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# report
# ──────────────────────────────────────────────────────────────────────────────


def render_markdown(meta: dict, res: dict) -> str:
    lines = [
        "# Liq-cascade exit replay — bot 40 (T-2026-KYT-9050-121)",
        "",
        f"_generated {meta['generated_utc']} · snapshot `{meta['snapshot']}` · "
        f"liq coverage {meta['liq_days']:.1f} days · marks unlevered %-points (channel view ≈ {LEV_VIEW:.0f}×)_",
        "",
        f"**{meta['verdict']}**",
        "",
    ]
    if "note" in res:
        lines.append(res["note"])
        return "\n".join(lines)
    b = res["book"]
    lines += [
        f"Book: n={b['n']} closed mirrors fully inside liq coverage, Σ mark {b['mark_sum']} "
        f"(baseline {b['per_slot_day_baseline']}/slot-day), span {b['span_utc'][0]} .. {b['span_utc'][1]}",
        "",
        "| variant | trig n (cov) | Σ realized | Σ counterfactual | Δ book | freed slot-d | slot credit | Δ incl. credit | val Δ | test Δ | agree |",
        "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|:--:|",
    ]
    for name, v in res["variants"].items():
        if v["n_triggered"] == 0:
            lines.append(f"| {name} | 0 | — | — | — | — | — | — | — | — | — |")
            continue
        lines.append(
            f"| {name} | {v['n_triggered']} ({v['coverage']:.0%}) | {v['realized_sum']} "
            f"| {v['counterfactual_sum']} | **{v['delta_sum']}** | {v['freed_slot_days']} "
            f"| {v['slot_credit']} | **{v['delta_incl_slot_credit']}** "
            f"| {v['halves']['val']['delta_sum']} | {v['halves']['test']['delta_sum']} "
            f"| {'✓' if v['halves_sign_agree'] else '✗'} |"
        )
    lines += ["", "## The T-052 question — whose exits does the rule replace? (Δ by realized close_reason)", ""]
    lines += ["| variant | reason | n | Σ realized | Σ counterfactual | Δ |", "|---|---|--:|--:|--:|--:|"]
    for name, v in res["variants"].items():
        for reason, r in v.get("by_close_reason", {}).items():
            lines.append(f"| {name} | {reason} | {r['n']} | {r['realized_sum']} | {r['cf_sum']} | {r['delta_sum']} |")
    lines += [
        "",
        "## Caveats",
        "",
        "- Counterfactual exit price = avg_price of the triggering forced order (a real print at that "
        "moment); a live Cornix market close would fill moments later — slippage unmodelled, both directions.",
        "- The slot credit prices freed slot-days at the SAME book's net-per-slot-day — a documented "
        "circularity (an honest forward test would re-price with the confirmation window's baseline).",
        "- liq_events is a 1 order/s/symbol SAMPLE; cascade cuts are the pre-registered T-120 cuts, "
        "reused unchanged. Cascades that completed before entry do not count (entry-gate territory).",
        "- Positions only count if their FULL life lies inside liq coverage — otherwise a cascade could "
        "have happened where we have no data (survivorship of the window, not of the book).",
        f"- Verdict discipline: Δ incl. slot credit must be positive in BOTH chrono halves; below "
        f"MIN_LIQ_DAYS={MIN_LIQ_DAYS} liq days nothing here is evidence.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--smoke", action="store_true", help="run below MIN_LIQ_DAYS (plumbing; NOT CONCLUDABLE)")
    ap.add_argument("--out-prefix", default=os.path.join("staging_models", "liq_exit_replay_study"))
    args = ap.parse_args()

    snap = read_snapshot(args.snapshot, tables=["trailing", "liq"])
    days = liq_coverage_days(snap["liq"])
    if days < MIN_LIQ_DAYS and not args.smoke:
        print(
            f"NOT CONCLUDABLE: liq_events covers only {days:.1f} days (< {MIN_LIQ_DAYS}). "
            "Re-run ~2026-08-24 or pass --smoke."
        )
        sys.exit(2)

    res = run_replay(snap["trailing"], snap["liq"])

    if days < MIN_LIQ_DAYS:
        verdict = (
            f"NOT CONCLUDABLE (plumbing run): liq coverage {days:.1f} < {MIN_LIQ_DAYS} days — "
            "numbers validate the pipeline, they are NOT evidence."
        )
    else:
        winners = [
            name
            for name, v in res.get("variants", {}).items()
            if v.get("n_triggered", 0) >= 20
            and v.get("delta_incl_slot_credit", 0) > 0
            and v.get("halves_sign_agree")
            and v["halves"]["val"]["delta_sum"] > 0
        ]
        verdict = (
            "CANDIDATE EXIT RULES (need a confirmation window): " + "; ".join(winners)
            if winners
            else "NO EDGE — no cascade-exit variant improves the book in both chrono halves."
        )

    meta = {
        "task": "T-2026-KYT-9050-121",
        "generated_utc": pd.Timestamp.now(tz="UTC").isoformat(),
        "snapshot": args.snapshot,
        "liq_days": days,
        "verdict": verdict,
        "params": {"LEV_VIEW": LEV_VIEW, "variants": [v[0] for v in VARIANTS], "cascades": [c[0] for c in CASCADES]},
    }
    os.makedirs(os.path.dirname(args.out_prefix) or ".", exist_ok=True)
    with open(args.out_prefix + ".json", "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "result": res}, f, indent=2, default=str)
    md = render_markdown(meta, res)
    with open(args.out_prefix + ".md", "w", encoding="utf-8") as f:
        f.write(md)
    print(md)
    print(f"\nwritten: {args.out_prefix}.md / .json")


if __name__ == "__main__":
    main()
