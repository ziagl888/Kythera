# tools/capital_split_backtest.py — does a 50:50 reserve split change the book?
"""Capital-split money-management variant of the T-105 portfolio simulation.

T-2026-KYT-9050-108, follow-up to T-105 (PR #275). Michi's proposal: 1000 EUR
total, split 50:50 into an *available* and a *locked reserve* bucket. Trades are
sized at 1 % of the available bucket (5 EUR at start). When a trade closes in
profit, 50 % of the profit moves into the reserve; when it closes at a loss,
50 % of the loss is refilled from the reserve back into the available bucket.

The analytic null hypothesis, stated before the run
---------------------------------------------------
With equal skim and refill fractions the transfer flows are symmetric: every
closed trade moves ``0.5 * pnl`` into each bucket, so as long as the reserve is
not exhausted and the refill is never capped, both buckets track
``start + 0.5 * cum_pnl`` exactly. The whole scheme is then arithmetically
identical to a single bucket sized at ``0.5 * size_frac`` of total equity —
EXCEPT that the reserve is dead margin: only the available half backs open
positions, so the margin ceiling sits at half the single-bucket level. The
simulation exists to confirm the equivalence on the real tape, to find where
the two admission paths diverge (margin rejections), and to price the variant
that is NOT a no-op: a one-way ratchet (skim without refill).

Everything upstream is T-105 verbatim: same export, same precompute, same
admission rules (weekly leg refit, 500-slot cap, exposure cap, margin), same
geometry variants. Nothing here touches the live database or the fleet.

Usage
-----
    python tools/capital_split_backtest.py --in reports/leg_composition_raw.npz \\
        --capital 1000 --leverage 5 --out reports/capital_split_backtest.json
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

import tools.leg_composition_replay as lcr  # noqa: E402
from tools.portfolio_backtest import (  # noqa: E402
    EXPOSURE_CAP,
    GEOMETRY_VARIANTS,
    SLOT_CAP,
    TRAIN_WEEKS,
    precompute,
    select_legs,
    simulate,
)

UTC = timezone.utc

EQUITY_POINTS = 500


def simulate_split(
    recs: list[dict],
    total_capital: float,
    leverage: float,
    *,
    split_frac: float = 0.5,
    size_frac: float = 0.01,
    skim_frac: float = 0.5,
    refill_frac: float = 0.5,
    fixed_size: float | None = None,
) -> dict:
    """Event-driven walk-forward run with two-bucket accounting.

    ``available`` is the bucket that backs margin and defines the trade size
    (``size_frac`` of its equity INCLUDING deployed margin, cost basis — there
    is no mark-to-market mid-trade in this record model). ``reserve`` holds the
    locked half. ``skim_frac`` of a win moves available -> reserve;
    ``refill_frac`` of a loss moves reserve -> available, capped at what the
    reserve still holds. ``fixed_size`` overrides the dynamic sizing with a
    constant EUR amount (the literal "5 EUR per trade" reading).
    """
    if not recs:
        return {}
    t_start = recs[0]["open_ts"]
    t_end = max(r["exit_ts"] for r in recs)
    week = 7 * 24 * 3600
    train_s = TRAIN_WEEKS * week

    avail = total_capital * split_frac  # bucket equity, incl. deployed margin
    reserve = total_capital * (1.0 - split_frac)
    deployed = 0.0
    open_pos: list[tuple[int, dict, float]] = []  # (exit_ts, record, size)
    n_long = n_short = 0
    taken = rejected_margin = rejected_leg = rejected_cap = rejected_slots = 0
    refill_capped = 0
    peak_occ = 0
    sizes: list[float] = []
    equity: list[tuple[int, float, float, float]] = []  # ts, total, avail, reserve
    daily: dict[str, float] = defaultdict(float)
    selection: set[tuple[str, str]] = set()
    next_refit = t_start + train_s
    first_tradeable = next_refit
    n_refits = 0

    events = sorted(recs, key=lambda r: r["open_ts"])
    ei = 0
    while ei < len(events) or open_pos:
        next_open = events[ei]["open_ts"] if ei < len(events) else None
        next_close = min((e for e, _, _ in open_pos), default=None)
        if next_open is None or (next_close is not None and next_close <= next_open):
            # close first: margin must be back before the next admission decision
            idx = min(range(len(open_pos)), key=lambda i: open_pos[i][0])
            _, rec, size = open_pos.pop(idx)
            deployed -= size
            pnl = size * leverage * rec["pnl_pct"] / 100.0
            avail += pnl
            if pnl > 0:
                skim = skim_frac * pnl
                avail -= skim
                reserve += skim
            elif pnl < 0:
                want = refill_frac * (-pnl)
                refill = min(reserve, want)
                refill_capped += refill < want
                reserve -= refill
                avail += refill
            n_long -= rec["direction"] == "LONG"
            n_short -= rec["direction"] == "SHORT"
            day = datetime.fromtimestamp(rec["exit_ts"], UTC).date().isoformat()
            daily[day] += pnl
            equity.append((rec["exit_ts"], avail + reserve, avail, reserve))
            continue

        r = events[ei]
        ei += 1
        if r["open_ts"] >= next_refit:
            selection = select_legs(recs, r["open_ts"] - train_s, r["open_ts"])
            next_refit = r["open_ts"] + week
            n_refits += 1
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
        size = fixed_size if fixed_size is not None else size_frac * avail
        free = avail - deployed
        if size <= 0 or free < size:
            rejected_margin += 1
            continue
        deployed += size
        open_pos.append((r["exit_ts"], r, size))
        sizes.append(size)
        n_long += r["direction"] == "LONG"
        n_short += r["direction"] == "SHORT"
        taken += 1
        peak_occ = max(peak_occ, len(open_pos))

    tradeable_days = max(0.0, (t_end - first_tradeable) / 86400)
    total_curve = [total_capital] + [t for _, t, _, _ in equity]
    peak = np.maximum.accumulate(total_curve)
    max_dd = float(((np.array(total_curve) - peak) / peak).min() * 100)
    avail_curve = [total_capital * split_frac] + [a for _, _, a, _ in equity]
    peak_a = np.maximum.accumulate(avail_curve)
    max_dd_avail = float(((np.array(avail_curve) - peak_a) / peak_a).min() * 100)
    days = sorted(daily.items())
    final_total = avail + reserve
    return {
        "total_capital": total_capital,
        "leverage": leverage,
        "split_frac": split_frac,
        "size_frac": size_frac,
        "skim_frac": skim_frac,
        "refill_frac": refill_frac,
        "fixed_size": fixed_size,
        "trades_taken": taken,
        "trades_per_day": round(taken / max(1e-9, tradeable_days), 1),
        "tradeable_days": round(tradeable_days, 2),
        "n_refits": n_refits,
        "rejected": {
            "leg": rejected_leg,
            "exposure_cap": rejected_cap,
            "slot_cap": rejected_slots,
            "margin": rejected_margin,
        },
        "binding_constraint": (
            "none"
            if max(rejected_slots, rejected_margin, rejected_cap) == 0
            else "slot_cap"
            if rejected_slots > max(rejected_margin, rejected_cap)
            else "margin"
            if rejected_margin > rejected_cap
            else "exposure_cap"
        ),
        "peak_occupancy": peak_occ,
        "size_mean": round(float(np.mean(sizes)), 4) if sizes else None,
        "size_min": round(float(np.min(sizes)), 4) if sizes else None,
        "size_max": round(float(np.max(sizes)), 4) if sizes else None,
        # `pnl_pct` arrives as np.float64 from precompute(), so plain arithmetic
        # here yields numpy scalars — cast, or json.dump refuses the artifact.
        "refill_capped_events": int(refill_capped),
        # Protection metrics for the T-109 sweep. `min_total_equity` is the
        # floor the whole account actually touched; `reserve_low_water` is how
        # far the "locked" bucket was drawn down — for a true ratchet
        # (refill_frac=0) it stays at its starting value by construction, and
        # any lower reading quantifies exactly how much protection the refill
        # rule gave away.
        "min_total_equity": round(float(np.min(total_curve)), 2),
        "reserve_low_water": round(
            float(min([total_capital * (1.0 - split_frac)] + [rv for _, _, _, rv in equity])), 2
        ),
        "final_available": round(float(avail), 2),
        "final_reserve": round(float(reserve), 2),
        "final_total": round(float(final_total), 2),
        "return_pct": round(float(final_total / total_capital - 1) * 100, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "max_drawdown_available_pct": round(max_dd_avail, 2),
        "worst_day": [
            min(days, key=lambda kv: kv[1])[0] if days else None,
            round(float(min((v for _, v in days), default=0.0)), 2),
        ],
        "losing_days": sum(1 for _, v in days if v < 0),
        "total_days": len(days),
        "equity_curve": [
            [int(ts), round(float(t), 2), round(float(a), 2), round(float(rv), 2)]
            for ts, t, a, rv in (
                [(t_start, total_capital, total_capital * split_frac, total_capital * (1 - split_frac))] + equity
            )[:: max(1, -(-len(equity) // EQUITY_POINTS))]
        ],
    }


def run_sweep(z, args, skims: list[float], refills: list[float]) -> dict[str, dict]:
    """The T-2026-KYT-9050-109 grid: every skim x refill pair, per geometry.

    T-108 pinned the two corners — symmetric (a disguised half-size single
    bucket) and pure ratchet (protection that starves the compounding base).
    The sweep prices the middle ground. Equity curves are dropped from the
    cells: 2 geometries x len(skims) x len(refills) runs would otherwise put
    500 points into every cell of a comparison table.
    """
    results: dict[str, dict] = {}
    for geom_name in (g.strip() for g in args.geometries.split(",")):
        geometry = GEOMETRY_VARIANTS[geom_name]
        print(
            f"\n=== geometry {geom_name} "
            f"(L {geometry['LONG'][0]:.0f}/{geometry['LONG'][1]:.0f}, "
            f"S {geometry['SHORT'][0]:.0f}/{geometry['SHORT'][1]:.0f}) ==="
        )
        recs = precompute(z, geometry, None, None)
        ref = simulate_split(
            recs, args.capital, args.leverage, split_frac=1.0, size_frac=0.005, skim_frac=0.0, refill_frac=0.0
        )
        ref.pop("equity_curve", None)
        results[f"{geom_name}|single_bucket_half_pct"] = ref
        print(
            f"  reference single bucket 0.5%:  ret {ref['return_pct']:+6.2f}%  "
            f"maxDD {ref['max_drawdown_pct']:6.2f}%  min_eq {ref['min_total_equity']:8.2f}"
        )
        print(f"  {'skim/refill':>12} " + " ".join(f"{r:>21.2f}" for r in refills))
        for skim in skims:
            cells = []
            for refill in refills:
                s = simulate_split(recs, args.capital, args.leverage, skim_frac=skim, refill_frac=refill)
                s.pop("equity_curve", None)
                results[f"{geom_name}|skim{skim:.2f}|refill{refill:.2f}"] = s
                cells.append(f"{s['return_pct']:+6.2f}% dd{s['max_drawdown_pct']:6.2f}%")
            print(f"  {skim:>12.2f} " + " ".join(f"{c:>21}" for c in cells))
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description="Capital-split money-management backtest (T-2026-KYT-9050-108)")
    ap.add_argument("--in", dest="infile", default="reports/leg_composition_raw.npz")
    ap.add_argument("--capital", type=float, default=1000.0)
    ap.add_argument("--leverage", type=float, default=5.0, help="matches the committed T-105 reference runs")
    ap.add_argument("--geometries", default="t104,symmetric_tight")
    ap.add_argument("--sweep", action="store_true", help="skim x refill grid (T-2026-KYT-9050-109)")
    ap.add_argument("--skims", default="0.25,0.5,0.75,1.0")
    ap.add_argument("--refills", default="0,0.25,0.5,0.75,1.0")
    ap.add_argument("--out", default="reports/capital_split_backtest.json")
    args = ap.parse_args()
    if args.sweep and args.out == "reports/capital_split_backtest.json":
        args.out = "reports/capital_split_sweep.json"

    z = np.load(args.infile, allow_pickle=True)
    src_meta = json.loads(str(z["meta"][0]))
    print(f"Loaded {len(z['s_ts']):,} signals from {args.infile} (export since {src_meta.get('since')})")

    # Same input gate as tools/portfolio_backtest.py: refuse a defective export
    # (T-107) rather than re-publishing its numbers under a new headline.
    fit = lcr._timestamp_domain_fit(
        c_sym=z["c_sym"],
        c_ts=z["c_ts"],
        c_high=z["c_high"],
        c_low=z["c_low"],
        s_sym=z["s_sym"],
        s_ts=z["s_ts"],
        s_entry=z["s_entry"],
    )
    print(f"Input timestamp-domain fit: {fit['rate']:.1%} ({fit['inside']:,}/{fit['checked']:,})")
    if fit["rate"] < lcr.DOMAIN_FIT_MIN:
        raise SystemExit(
            f"Input export FAILS the timestamp-domain check ({fit['rate']:.1%} < "
            f"{lcr.DOMAIN_FIT_MIN:.0%}). Re-export before simulating; do not lower the threshold."
        )

    if args.sweep:
        results = run_sweep(
            z, args, [float(s) for s in args.skims.split(",")], [float(r) for r in args.refills.split(",")]
        )
    else:
        # The variants. `single_bucket_half_pct` is the analytic twin of the split
        # scheme (0.5 % of total equity, one bucket) — if the equivalence argument
        # holds on the real tape, those two rows must only diverge through margin
        # admission. `t105_fixed_5eur` anchors everything to the T-105 baseline.
        results = {}
        for geom_name in (g.strip() for g in args.geometries.split(",")):
            geometry = GEOMETRY_VARIANTS[geom_name]
            print(
                f"\n=== geometry {geom_name} "
                f"(L {geometry['LONG'][0]:.0f}/{geometry['LONG'][1]:.0f}, "
                f"S {geometry['SHORT'][0]:.0f}/{geometry['SHORT'][1]:.0f}) ==="
            )
            recs = precompute(z, geometry, None, None)

            variants: dict[str, dict] = {}
            variants["t105_fixed_5eur"] = simulate(recs, args.capital, 5.0, args.leverage)
            variants["split_50_50_dynamic"] = simulate_split(recs, args.capital, args.leverage)
            variants["split_50_50_fixed5"] = simulate_split(recs, args.capital, args.leverage, fixed_size=5.0)
            variants["single_bucket_half_pct"] = simulate_split(
                recs, args.capital, args.leverage, split_frac=1.0, size_frac=0.005, skim_frac=0.0, refill_frac=0.0
            )
            variants["split_ratchet_no_refill"] = simulate_split(recs, args.capital, args.leverage, refill_frac=0.0)

            for label, s in variants.items():
                ret = s.get("return_pct")
                dd = s.get("max_drawdown_pct")
                print(
                    f"  {label:24s} taken={s.get('trades_taken', 0):>5} "
                    f"ret={ret:+7.2f}%  maxDD={dd:7.2f}%  bind={s.get('binding_constraint', '-'):12s} "
                    f"final={s.get('final_total', s.get('final_balance')):>8}"
                )
                results[f"{geom_name}|{label}"] = s

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "task": "T-2026-KYT-9050-109" if args.sweep else "T-2026-KYT-9050-108",
                "input": os.path.basename(args.infile),
                "input_since": src_meta.get("since"),
                "input_domain_fit": round(fit["rate"], 4),
                "capital": args.capital,
                "leverage": args.leverage,
                "results": results,
            },
            fh,
            indent=1,
        )
    print(f"\nWritten: {args.out}")


if __name__ == "__main__":
    main()
