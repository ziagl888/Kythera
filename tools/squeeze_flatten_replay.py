# tools/squeeze_flatten_replay.py — flatten ALL against-side bot-40 positions at squeeze onset?
"""Counterfactual replay of Michi's rule (2026-08-09): the moment a MARKET-WIDE
short squeeze fires, close ALL open shorts immediately (long flush → all
longs). T-2026-KYT-9050-123 — lifts the T-122 scope cut.

DB-FREE: reads the T-120/T-123 DuckDB snapshot. Episodes are re-derived from
the liq table with the SHARED detector (tools/funding_liq_gate_study —
pre-registered H3s/H3l cuts, single source); counterfactual close prices come
from the snapshot's `ticker_slices` (targeted ticker_10s pulls around the
episodes, added to the export in T-123 — older snapshots need a re-export).

Pricing honesty: ticker_10s is a ~40s-sampled, GAPPY tape (T-035 discovery —
useless for touch detection, fine for a mark). The counterfactual close is the
symbol's last print at or before the onset within PRICE_TOL_S; positions
without a covered print are EXCLUDED and counted (`no_price`), never guessed.

Onset semantics: minute m's breadth covers (m−15min, m], so the squeeze is
observable at the END of minute m — the replay closes at onset = episode
start + 1 minute (no lookahead; a live monitor acts one minute after the
window completes). First-episode-wins per position: once flattened, a position
cannot be flattened again by a later episode.

Accounting = T-121 conventions (shared code): Δ vs realized mark, freed
slot-days credited at the book's net-per-slot-day (T-042 metric, documented
circularity), per-close_reason decomposition (the T-052 trap — "SL damage
avoided" vs "TRAIL wins truncated" shown separately). The tension this replay
decides: closing a short INTO the squeeze buys back at spiked prices — early
exit only pays if the squeeze continues.

Invocation:
  python tools/squeeze_flatten_replay.py --snapshot .local/gate_snapshots/gate_snapshot_YYYYMMDD.duckdb [--smoke]
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
    MIN_LIQ_DAYS,
    liq_coverage_days,
    market_breadth_minutes,
    squeeze_episodes,
)
from tools.gate_snapshot import read_snapshot  # noqa: E402
from tools.liq_exit_replay_study import (  # noqa: E402
    _NS_PER_DAY,
    mark_pct,
    per_slot_day_baseline,
    prepare_book,
    summarize_variant,
)

TRIGGER_LAG_MIN = 1  # act at the close of the minute whose breadth completed the cut
PRICE_TOL_S = 180  # last print at most this far before onset counts as the mark
FLATTEN_SIDE = {"SHORT_SQUEEZE": "SHORT", "LONG_FLUSH": "LONG"}


def ticker_arrays(ticker: pd.DataFrame) -> dict[str, tuple[np.ndarray, np.ndarray]]:
    """symbol → (ts_ns ascending, price). Prints without a price are useless."""
    t = ticker.dropna(subset=["price"]).sort_values("ts")
    return {
        str(s): (g["ts"].values.astype("datetime64[ns]").astype("int64"), g["price"].astype(float).to_numpy())
        for s, g in t.groupby("symbol", sort=False)
    }


def asof_price(pack: tuple[np.ndarray, np.ndarray] | None, t_ns: int, tol_ns: int) -> float | None:
    """Last print at or before ``t_ns`` within ``tol_ns``; None when uncovered."""
    if pack is None:
        return None
    ts, px = pack
    i = int(np.searchsorted(ts, t_ns, side="right")) - 1
    if i < 0 or t_ns - ts[i] > tol_ns:
        return None
    return float(px[i])


def flatten_replay(book: pd.DataFrame, episodes: pd.DataFrame, ticker: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """One pass over all episodes, chronological, first-episode-wins.

    Returns (flattened rows in the T-121 accounting schema, stats incl.
    per-episode counts and the no_price exclusions)."""
    tick = ticker_arrays(ticker)
    tol_ns = PRICE_TOL_S * 1_000_000_000
    dirn = book["direction"].str.upper()
    flattened: set = set()
    rows, ep_stats = [], []
    no_price = 0
    for ep in episodes.sort_values("start").itertuples(index=False):
        side = FLATTEN_SIDE[ep.side]
        onset = ep.start + pd.Timedelta(minutes=TRIGGER_LAG_MIN)
        onset_ns = int(onset.value)
        open_now = book[
            (dirn == side) & (book["opened_at"] < onset) & (book["closed_at"] > onset) & ~book["id"].isin(flattened)
        ]
        hit_n = miss_n = 0
        for pos in open_now.itertuples(index=False):
            px = asof_price(tick.get(str(pos.symbol)), onset_ns, tol_ns)
            if px is None:
                no_price += 1
                miss_n += 1
                continue
            cf = mark_pct(pos.direction, float(pos.entry), px)
            flattened.add(pos.id)
            hit_n += 1
            rows.append(
                {
                    "id": pos.id,
                    "direction": pos.direction.upper(),
                    "close_reason": pos.close_reason,
                    "opened_ns": pos.opened_ns,
                    "realized": float(pos.close_mark_pct),
                    "counterfactual": cf,
                    "delta": cf - float(pos.close_mark_pct),
                    "freed_days": (pos.closed_ns - onset_ns) / _NS_PER_DAY,
                }
            )
        ep_stats.append(
            {"side": ep.side, "start": str(ep.start), "end": str(ep.end), "flattened": hit_n, "no_price": miss_n}
        )
    return pd.DataFrame(rows), {"episodes": ep_stats, "no_price_total": no_price}


def render_markdown(meta: dict, res: dict) -> str:
    lines = [
        "# Squeeze-flatten replay — bot 40 (T-2026-KYT-9050-123)",
        "",
        f"_generated {meta['generated_utc']} · snapshot `{meta['snapshot']}` · "
        f"liq coverage {meta['liq_days']:.1f} days · marks unlevered %-points_",
        "",
        f"**{meta['verdict']}**",
        "",
    ]
    if "note" in res:
        lines.append(res["note"])
        return "\n".join(lines)
    b = res["book"]
    lines.append(
        f"Book: n={b['n']} closed mirrors fully inside liq coverage, Σ mark {b['mark_sum']} "
        f"(baseline {b['per_slot_day_baseline']}/slot-day) · episodes: {res['n_episodes']} "
        f"({res['n_short_squeeze']} short-squeeze / {res['n_long_flush']} long-flush) · "
        f"positions without a covered price at onset: {res['stats']['no_price_total']}"
    )
    lines.append("")
    lines.append("| episode | side | flattened | no price |")
    lines.append("|---|---|--:|--:|")
    for e in res["stats"]["episodes"]:
        lines.append(f"| {e['start']} .. {e['end']} | {e['side']} | {e['flattened']} | {e['no_price']} |")
    lines.append("")
    v = res["summary"]
    if v.get("n_triggered", 0) == 0:
        lines.append("No position was flattened — nothing to account.")
    else:
        lines.append(
            f"Flattened n={v['n_triggered']} ({v['coverage']:.0%} of book): Σ realized {v['realized_sum']} → "
            f"Σ counterfactual {v['counterfactual_sum']} · **Δ {v['delta_sum']}** · freed {v['freed_slot_days']} "
            f"slot-d · slot credit {v['slot_credit']} · **Δ incl. credit {v['delta_incl_slot_credit']}** · "
            f"val Δ {v['halves']['val']['delta_sum']} / test Δ {v['halves']['test']['delta_sum']} "
            f"(agree: {v['halves_sign_agree']})"
        )
        lines.append("")
        lines.append("### The T-052 question — whose exits does the flatten replace?")
        lines.append("")
        lines.append("| reason | n | Σ realized | Σ counterfactual | Δ |")
        lines.append("|---|--:|--:|--:|--:|")
        for reason, r in v.get("by_close_reason", {}).items():
            lines.append(f"| {reason} | {r['n']} | {r['realized_sum']} | {r['cf_sum']} | {r['delta_sum']} |")
    lines += [
        "",
        "## Caveats",
        "",
        "- Counterfactual price = last ticker_10s print ≤ onset (tol 180 s). The tape is ~40s-sampled and "
        "gappy (T-035) — fine for a mark, useless for touches; uncovered positions are excluded and counted.",
        "- Onset = episode start + 1 min (breadth of minute m is known at its END — no lookahead).",
        "- Slot credit prices freed slot-days at the SAME book's net-per-slot-day (documented circularity).",
        "- Episodes are the pre-registered T-122 cuts (shared code); first-episode-wins per position.",
        f"- Below MIN_LIQ_DAYS={MIN_LIQ_DAYS} liq days nothing here is evidence.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--snapshot", required=True)
    ap.add_argument("--smoke", action="store_true", help="run below MIN_LIQ_DAYS (plumbing; NOT CONCLUDABLE)")
    ap.add_argument("--out-prefix", default=os.path.join("staging_models", "squeeze_flatten_replay"))
    args = ap.parse_args()

    snap = read_snapshot(args.snapshot)
    if "ticker_slices" not in snap:
        print("snapshot has no ticker_slices table — re-export with the T-123 tools/gate_snapshot_export.py")
        sys.exit(2)
    liq = snap["liq"]
    days = liq_coverage_days(liq)
    if days < MIN_LIQ_DAYS and not args.smoke:
        print(
            f"NOT CONCLUDABLE: liq_events covers only {days:.1f} days (< {MIN_LIQ_DAYS}). "
            "Re-run ~2026-08-24 or pass --smoke."
        )
        sys.exit(2)

    book = prepare_book(snap["trailing"])
    lo, hi = liq["ts"].min(), liq["ts"].max()
    book = book[(book["opened_at"] >= lo) & (book["closed_at"] <= hi)]
    episodes = squeeze_episodes(market_breadth_minutes(liq))

    if not len(book):
        res: dict = {"note": "no positions fully inside liq_events coverage"}
    else:
        trig, stats = flatten_replay(book, episodes, snap["ticker_slices"])
        psd = per_slot_day_baseline(book)
        res = {
            "book": {
                "n": int(len(book)),
                "mark_sum": round(float(book["close_mark_pct"].sum()), 2),
                "per_slot_day_baseline": round(psd, 4),
            },
            "n_episodes": int(len(episodes)),
            "n_short_squeeze": int((episodes["side"] == "SHORT_SQUEEZE").sum()),
            "n_long_flush": int((episodes["side"] == "LONG_FLUSH").sum()),
            "stats": stats,
            "summary": summarize_variant(trig, book, psd),
        }

    if days < MIN_LIQ_DAYS:
        verdict = (
            f"NOT CONCLUDABLE (plumbing run): liq coverage {days:.1f} < {MIN_LIQ_DAYS} days — "
            "numbers validate the pipeline, they are NOT evidence."
        )
    else:
        v = res.get("summary", {})
        good = (
            v.get("n_triggered", 0) >= 20
            and v.get("delta_incl_slot_credit", 0) > 0
            and v.get("halves_sign_agree")
            and v["halves"]["val"]["delta_sum"] > 0
        )
        verdict = (
            "CANDIDATE: squeeze-flatten improves the book in both chrono halves (needs a confirmation window)."
            if good
            else "NO EDGE — flattening into market squeezes does not improve the book in both chrono halves."
        )

    meta = {
        "task": "T-2026-KYT-9050-123",
        "generated_utc": pd.Timestamp.now(tz="UTC").isoformat(),
        "snapshot": args.snapshot,
        "liq_days": days,
        "verdict": verdict,
        "params": {"TRIGGER_LAG_MIN": TRIGGER_LAG_MIN, "PRICE_TOL_S": PRICE_TOL_S},
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
