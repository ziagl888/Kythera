"""T-2026-KYT-9050-042 (Phase C) — which legs fit into the trailing bot's own channel?

The trailing bot posts into ONE new Telegram channel, and Cornix caps a channel at
**500 simultaneously open trades** (operator, Michi 2026-07-26). That cap — not the
per-trade Sharpe — makes this a selection problem: a leg only deserves a seat if it
earns more per *occupied slot-day* than the leg it displaces. On hold behaviour
MIS1-72h LONG alone draws ~283 of the 500 seats.

Trailing moves both sides of that trade-off (result per trade AND holding time), so
it has to be simulated. Two measurement traps had to be fixed before the numbers
meant anything, and both are baked into the pins:

  * **Same-candle trigger.** `wave_buildup_study.trail_capture` updates the peak and
    tests the retrace on the SAME candle, so a candle straddling the entry both arms
    and breaches the trail — the trade exits on its own first candle. T-041 could
    live with that (it only used the resulting return, and documented the optimism);
    here the exit TIME is the whole point, so the peak must be strictly prior.
  * **A scale-free trail is a micro-scalper.** "Close on a 10 % give-back of the peak"
    triggers on a trade that was +0.5 % up and gave back 0.05 %. With 15m candles
    ordinary noise closes almost everything within one candle: EPD1 SHORT collapses
    from Ø 3.59 % per trade over 69h to ~1 % within minutes. That is also WHY trailing
    looked so good in T-041/T-046 — the variance collapses because every trade becomes
    a micro-win. The fix is an **activation floor**: the trail only arms once the trade
    is meaningfully in profit (`core.wave_exit_sim.trailing_tp_trigger` has the same
    parameter, left at 0 there). This tool sweeps it instead of guessing.

Values are reported **net of fees** at the repo's convention (0.10 % taker round-trip
per trade, `tools/audit/step4_results.py`), because a micro-exit strategy is
artificially flattered by a gross comparison.

Everything is scored per **(tag, direction) leg** against `shadow_gate.leg_status`,
never per tag or per script: the register switches per leg, and aggregating over both
directions mixes live legs with shadow ones that never reach Cornix. That error makes
healthy live legs look like retirement candidates (BR aggregates to −4012 % while
BR1H LONG, its live leg, earns +4889 %).

Read-only: SELECTs against closed_ai_signals + candles, no writes, no live effect.

Invariants:
  * Causal — a trade's trailing exit is decided only by candles strictly before it.
  * Trailing can only pull an exit EARLIER: a trade never outlives its recorded close.
  * An underwater trade never trails out; the activation floor only delays arming.
  * `closed_ai_signals` carries no entry2, so realised values are already
    single-entry (arm B) and match the posting after PR #197.

Usage (serial — one sim job at a time on the live VPS):
    python tools/trailing_slot_budget.py --start 2026-03-01 --tf 15m --x 0.10
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import timedelta

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from tools.wave_buildup_study import (  # noqa: E402
    CLAMP,
    DEFAULT_OUT_DIR,
    load_trades,
    read_coin_wick,
)

HOUR = np.timedelta64(1, "h")
DEFAULT_BUDGET = 500  # Cornix per-channel cap on simultaneously open trades
DEFAULT_ACTIVATIONS = [0.0, 1.0, 2.0, 5.0, 10.0]  # unlevered % peak before the trail arms
FEE_RT = 0.10  # taker round-trip %, repo convention (tools/audit/step4_results.py)
THIN_N = 30  # below this a leg's sign is not trusted


def prior_peak(fav: np.ndarray) -> np.ndarray:
    """Running favourable peak established STRICTLY BEFORE each candle.

    Within one candle the order of high and low is unknowable, so a trail may only
    fire against a peak already on the tape. Index 0 therefore never triggers.
    """
    if len(fav) == 0:
        return fav
    return np.concatenate(([-np.inf], np.maximum.accumulate(fav)[:-1]))


def trail_exit(fav: np.ndarray, adv: np.ndarray, x: float, activation: float = 0.0) -> int | None:
    """First candle where the trade gives back `x` of a previously established peak,
    once that peak is above `activation`. None if it never triggers (the trade then
    runs to its recorded close)."""
    if len(fav) < 2:
        return None
    peak = prior_peak(fav)
    hit = (peak > activation) & (adv <= peak * (1.0 - x))
    if not hit.any():
        return None
    return int(np.argmax(hit))


def capture_at(
    fav: np.ndarray, x: float, k: int | None, lev: float, hold_ru: float, hold_rl: float
) -> tuple[float, float]:
    """Realised (unlev, lev) move of a trailing exit at `k`; the recorded hold values
    if it never triggered. The fill is the stop level on the peak that armed it."""
    if k is None:
        return hold_ru, hold_rl
    tm = float(prior_peak(fav)[k] * (1.0 - x))
    return tm, max(tm * lev, CLAMP)


def _naive(dt):
    return dt.replace(tzinfo=None) if getattr(dt, "tzinfo", None) else dt


def simulate(conn, trades: list[dict], lev: float, tf: str, x: float, activations: list[float]) -> int:
    """Attach a trailing outcome per activation level to every trade, in place.

    Sets `t["trail"][a] = (ru, rl, exit_time)`. One candle pass covers all activation
    levels — the DB read is the expensive part, the sweep is vectorised on top.
    Trades without candle coverage keep their recorded (hold) outcome.
    """
    import time

    by_coin: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        by_coin[t["sym"]].append(t)

    t0 = time.time()
    no_candle = 0
    for ci, (sym, tl) in enumerate(sorted(by_coin.items()), 1):
        lo = min(t["ot"] for t in tl) - timedelta(hours=2)
        hi = max(t["ct"] for t in tl) + timedelta(hours=2)
        cd = read_coin_wick(conn, sym, lo, hi, tf)
        # a coin without candles yields {"t": empty} and no h/l — probe before unpacking
        covered = len(cd["t"]) > 0
        ct_all, hi_all, lo_all = (cd["t"], cd["h"], cd["l"]) if covered else (cd["t"], None, None)
        for t in tl:
            hold = (t["real_unlev"], t["real_lev"], _naive(t["ct"]))
            t["trail"] = dict.fromkeys(activations, hold)
            if not covered:
                no_candle += 1
                continue
            m = (ct_all >= np.datetime64(_naive(t["ot"]))) & (ct_all <= np.datetime64(_naive(t["ct"])))
            hh, ll, tt = hi_all[m], lo_all[m], ct_all[m]
            if len(hh) == 0:
                no_candle += 1
                continue
            e, is_long = t["entry"], t["dir"] == "LONG"
            fav = ((hh - e) / e * 100.0) if is_long else ((e - ll) / e * 100.0)
            adv = ((ll - e) / e * 100.0) if is_long else ((e - hh) / e * 100.0)
            for a in activations:
                k = trail_exit(fav, adv, x, a)
                ru, rl = capture_at(fav, x, k, lev, t["real_unlev"], t["real_lev"])
                if k is None:
                    t["trail"][a] = (ru, rl, _naive(t["ct"]))
                else:
                    exit_at = tt[k].astype("datetime64[us]").astype(object)
                    t["trail"][a] = (ru, rl, min(exit_at, _naive(t["ct"])))
        if ci % 200 == 0 or ci == len(by_coin):
            print(f"  [{ci}/{len(by_coin)}] {time.time() - t0:.0f}s", flush=True)
    return no_candle


def slot_index(sel: list[dict], grid0: np.datetime64, glen: int, act: float | None) -> tuple[np.ndarray, np.ndarray]:
    """Per-trade (start, end) hour indices on the common grid. `act=None` = hold."""
    starts, ends = [], []
    for t in sel:
        end = t["ct"] if act is None else t["trail"][act][2]
        a = int((np.datetime64(_naive(t["ot"]), "h") - grid0) / HOUR)
        b = int((np.datetime64(_naive(end), "h") - grid0) / HOUR)
        a, b = max(0, min(a, glen)), max(0, min(b, glen))
        if b <= a:
            b = a + 1  # a sub-hour trade still occupies its hour
        starts.append(a)
        ends.append(b)
    return np.asarray(starts, dtype=np.int64), np.asarray(ends, dtype=np.int64)


def occupancy(starts: np.ndarray, ends: np.ndarray, glen: int) -> np.ndarray:
    """Hourly count of simultaneously open positions (vectorised diff + cumsum)."""
    d = np.zeros(glen + 1, dtype=np.int64)
    if len(starts):
        np.add.at(d, starts, 1)
        np.add.at(d, ends, -1)
    return np.cumsum(d[:-1])


def leg_stats(trades: list[dict], live_only: bool, act: float, fee: float) -> tuple[dict, np.datetime64, int]:
    """Per (tag, direction) leg: net value + slot draw, hold vs trailing at `act`."""
    from core import shadow_gate as sg

    legs: dict[str, list[dict]] = defaultdict(list)
    status: dict[str, str] = {}
    for t in trades:
        st = sg.leg_status(t["tag"], t["dir"])
        if live_only and st != sg.LIVE:
            continue
        key = f"{t['tag']} {t['dir']}"
        legs[key].append(t)
        status[key] = st

    lo = min(_naive(t["ot"]) for t in trades)
    hi = max(max(_naive(t["ct"]), t["trail"][act][2]) for t in trades)
    grid0 = np.datetime64(lo, "h")
    glen = int((np.datetime64(hi, "h") - grid0) / HOUR) + 1

    out = {}
    for key, sel in legs.items():
        s_h, e_h = slot_index(sel, grid0, glen, None)
        s_t, e_t = slot_index(sel, grid0, glen, act)
        occ_h, occ_t = occupancy(s_h, e_h, glen), occupancy(s_t, e_t, glen)
        hold_h = np.array([(_naive(t["ct"]) - _naive(t["ot"])).total_seconds() / 3600 for t in sel])
        trail_h = np.array([(t["trail"][act][2] - _naive(t["ot"])).total_seconds() / 3600 for t in sel])
        ru = np.array([t["trail"][act][0] for t in sel])
        v_hold = float(sum(t["real_unlev"] for t in sel) - fee * len(sel))
        v_trail = float(ru.sum() - fee * len(sel))
        sd_trail = trail_h.sum() / 24
        out[key] = {
            "status": status[key],
            "n": len(sel),
            "value_hold_net": round(v_hold, 1),
            "value_trail_net": round(v_trail, 1),
            "per_trade_trail_net": round(float(ru.mean() - fee), 3),
            "below_fee_pct": round(float((ru < fee).mean() * 100), 1),
            "slot_days_trail": round(sd_trail, 1),
            "density_trail": round(v_trail / sd_trail, 4) if sd_trail else 0.0,
            "occ_mean_hold": round(float(occ_h.mean()), 1),
            "occ_mean_trail": round(float(occ_t.mean()), 1),
            "occ_p95_trail": round(float(np.percentile(occ_t, 95)), 1),
            "hold_h_median": round(float(np.median(hold_h)), 1),
            "trail_h_median": round(float(np.median(trail_h)), 2),
            "thin": len(sel) < THIN_N,
            "_idx": (s_t, e_t),
            "_ru": ru,
            "_n": len(sel),
        }
    return out, grid0, glen


def fill_channel(stats: dict, glen: int, budget: int, measure: str, fee: float) -> dict:
    """Greedy fill of the channel by net result per slot-day, joint occupancy exact."""
    cands = [k for k, s in stats.items() if s["density_trail"] > 0 and not s["thin"]]
    cands.sort(key=lambda k: -stats[k]["density_trail"])

    accepted: list[str] = []
    s_pool = np.array([], dtype=np.int64)
    e_pool = np.array([], dtype=np.int64)
    value = 0.0
    n = 0
    joint = np.zeros(glen)
    rejected = []
    for k in cands:
        s_try = np.concatenate([s_pool, stats[k]["_idx"][0]])
        e_try = np.concatenate([e_pool, stats[k]["_idx"][1]])
        occ = occupancy(s_try, e_try, glen)
        load = float(occ.mean()) if measure == "mean" else float(np.percentile(occ, 95))
        if load <= budget:
            accepted.append(k)
            s_pool, e_pool, joint = s_try, e_try, occ
            value += float(stats[k]["_ru"].sum() - fee * stats[k]["_n"])
            n += stats[k]["_n"]
        else:
            rejected.append({"leg": k, "would_be": round(load, 1)})
    return {
        "measure": measure,
        "budget": budget,
        "accepted": accepted,
        "rejected": rejected,
        "joint_mean": round(float(joint.mean()), 1) if n else 0.0,
        "joint_p95": round(float(np.percentile(joint, 95)), 1) if n else 0.0,
        "joint_max": int(joint.max()) if n else 0,
        "value_trail_net": round(value, 1),
        "n": n,
    }


def render_md(meta: dict) -> str:
    L = [
        f"# Trailing-Bot Slot-Budget — welche Beine in den neuen Channel? ({meta['task']})",
        "",
        f"_generated {meta['generated_at']} · read-only · nur LIVE-Beine · trailing X={int(meta['x'] * 100)} % · "
        f"tf {meta['tf']} · {meta['lev']:.0f}x · ab {meta['start']} · Gebühr {meta['fee']:.2f} %/Trade_",
        "",
        f"**Frage:** Cornix deckelt einen Channel bei **{meta['budget']} gleichzeitig offenen Trades**. "
        "Maßstab ist der **Netto-Ertrag pro belegtem Slot-Tag**, nicht der per-Trade-Sharpe. "
        "Bewertet pro **(Tag, Richtung)**, weil `shadow_gate` pro Bein schaltet.",
        "",
        "## Aktivierungsschwelle — der entscheidende Parameter",
        "",
        "Ein skalenfreier Trail (`schließe bei X % Rückgabe vom Peak`) feuert auch auf einem "
        "0,5-%-Peak und macht die Fleet zum Micro-Scalper. `act` = geforderter Peak (unlev %), "
        "bevor der Trail scharf wird. `unter Gebühr` = Anteil Trades, deren Trailing-Ertrag die "
        "0,10-%-Gebühr nicht deckt.",
        "",
        "| act % | Σ netto (alle Live-Beine) | Ø/Trade netto | unter Gebühr | Median Haltedauer h | "
        "Ø Slots (alle) | netto/Slot | **Füllung 500 netto** | Beine |",
        "|--:|--:|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for a in meta["activations"]:
        s = meta["sweep"][str(a)]
        per_slot = s["total_net"] / s["occ_mean_all"] if s["occ_mean_all"] else 0
        L.append(
            f"| {a:.0f} | {s['total_net']:.0f} | {s['per_trade_net']:.3f} | {s['below_fee_pct']:.0f} % | "
            f"{s['median_hold_h']:.1f} | {s['occ_mean_all']:.0f} | {per_slot:.0f} | "
            f"**{s['fill_mean_net']:.0f}** | {s['fill_mean_legs']} |"
        )
    L += [
        "",
        f"_Referenz hold (ohne Trailing), netto: **{meta['hold_total_net']:.0f}** · "
        f"Ø Slots {meta['hold_occ_mean']:.0f} · Median Haltedauer {meta['hold_median_h']:.1f} h_",
        "",
        f"## Beine bei act = {meta['chosen_act']:.0f} %",
        "",
        "| Bein | n | Ø Slots hold → trail | p95 | Haltedauer h hold → trail | Σ netto hold → trail | **netto/Slot-Tag** | unter Gebühr |",
        "|---|--:|--:|--:|--:|--:|--:|--:|",
    ]
    s = meta["legs"]
    for k in sorted(s, key=lambda k: -s[k]["density_trail"]):
        g = s[k]
        thin = " ⚠dünn" if g["thin"] else ""
        L.append(
            f"| {k}{thin} | {g['n']} | {g['occ_mean_hold']:.0f} → **{g['occ_mean_trail']:.0f}** | "
            f"{g['occ_p95_trail']:.0f} | {g['hold_h_median']:.0f} → {g['trail_h_median']:.1f} | "
            f"{g['value_hold_net']:.0f} → {g['value_trail_net']:.0f} | **{g['density_trail']:.3f}** | "
            f"{g['below_fee_pct']:.0f} % |"
        )
    L += ["", "## Kanal-Füllung (greedy nach Netto-Dichte; Gleichzeitigkeit exakt gerechnet)", ""]
    for v in meta["selections"]:
        L += [
            f"### Budget {v['budget']} nach **{v['measure']}**",
            "",
            f"- **Aufgenommen ({len(v['accepted'])}):** {', '.join(v['accepted']) or '—'}",
            f"- Belegung: Ø {v['joint_mean']} · p95 {v['joint_p95']} · max {v['joint_max']}",
            f"- Σ netto: **{v['value_trail_net']:.0f} %** ({v['n']} Trades)",
            "- Abgelehnt: " + (", ".join(f"{r['leg']} ({r['would_be']:.0f})" for r in v["rejected"]) or "—"),
            "",
        ]
    L += [
        "## Ehrliche Grenzen",
        "",
        "- **Registerstand ist zeit-variabel** — der heutige `leg_status` liegt auf der ganzen Historie.",
        "- **15m-Auflösung.** Der Trail wird auf Kerzen-Extremen ausgewertet, mit strikt vorherigem "
        "Peak (keine Gleich-Kerzen-Trigger). Ein 5m/10s-Resolver (T-035-Harness) ist die nächste "
        "Verschärfung; die DCA-treue Bestätigung der Finalisten steht noch aus.",
        "- **Wert = Σ unlevered %-Bewegung minus Gebühr**, Gleichgewichtung, kein Compounding: als "
        "Dichte-Maß robust, als absolute Rendite nicht wörtlich.",
        "- **Slippage ist NICHT modelliert.** Bei niedriger Aktivierungsschwelle sind die Exits klein "
        "und zahlreich — dort frisst Slippage überproportional. Das spricht zusätzlich gegen act=0.",
        "- Greedy ist nicht beweisbar optimal (Knapsack).",
    ]
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", default="2026-03-01")
    ap.add_argument("--tf", default="15m")
    ap.add_argument("--x", type=float, default=0.10, help="trailing retrace fraction")
    ap.add_argument("--lev", type=float, default=20.0)
    ap.add_argument("--budget", type=int, default=DEFAULT_BUDGET)
    ap.add_argument("--fee", type=float, default=FEE_RT, help="taker round-trip %% per trade")
    ap.add_argument("--activations", default=",".join(str(a) for a in DEFAULT_ACTIVATIONS))
    ap.add_argument("--all-legs", action="store_true", help="score shadow/silent legs too")
    ap.add_argument("--out", default=DEFAULT_OUT_DIR)
    args = ap.parse_args()

    from datetime import datetime, timezone

    from core.database import get_db_connection

    acts = [float(a) for a in args.activations.split(",")]
    conn = get_db_connection()
    trades = load_trades(conn, ["ALL"], lev=args.lev, start=args.start)
    print(f"loaded {len(trades)} trades since {args.start}", flush=True)
    no_candle = simulate(conn, trades, args.lev, args.tf, args.x, acts)
    conn.close()
    print(f"simulated; {no_candle} trades without candle coverage", flush=True)

    sweep, per_act, per_fill = {}, {}, {}
    for a in acts:
        stats, grid0, glen = leg_stats(trades, live_only=not args.all_legs, act=a, fee=args.fee)
        s_all = np.concatenate([v["_idx"][0] for v in stats.values()])
        e_all = np.concatenate([v["_idx"][1] for v in stats.values()])
        ru_all = np.concatenate([v["_ru"] for v in stats.values()])
        occ_all = occupancy(s_all, e_all, glen)
        holds = [v["trail_h_median"] for v in stats.values()]
        sweep[str(a)] = {
            "total_net": round(float(ru_all.sum() - args.fee * len(ru_all)), 1),
            "per_trade_net": round(float(ru_all.mean() - args.fee), 4),
            "below_fee_pct": round(float((ru_all < args.fee).mean() * 100), 1),
            "median_hold_h": round(float(np.median(holds)), 2),
            "occ_mean_all": round(float(occ_all.mean()), 1),
        }
        per_act[a] = (stats, glen)
        # The channel — not the fleet — is what has to be maximised, so every
        # activation gets its own greedy fill. Picking the activation with the highest
        # TOTAL net would be the wrong objective: act=10 nets the most fleet-wide but
        # draws ~948 average seats, nearly twice the cap, while a low floor is far more
        # net-per-seat efficient. Only the fill under the budget settles it.
        fills = {m: fill_channel(stats, glen, args.budget, m, args.fee) for m in ("mean", "p95")}
        sweep[str(a)]["fill_mean_net"] = fills["mean"]["value_trail_net"]
        sweep[str(a)]["fill_mean_legs"] = len(fills["mean"]["accepted"])
        sweep[str(a)]["fill_p95_net"] = fills["p95"]["value_trail_net"]
        sweep[str(a)]["fill_p95_legs"] = len(fills["p95"]["accepted"])
        per_fill[a] = fills
        print(
            f"  act={a}: net {sweep[str(a)]['total_net']:.0f}, Ø slots {sweep[str(a)]['occ_mean_all']:.0f}, "
            f"fill(mean) {fills['mean']['value_trail_net']:.0f} aus {len(fills['mean']['accepted'])} Beinen",
            flush=True,
        )

    # The operating point is the activation whose p95-SAFE fill nets the most. p95 is
    # the honest criterion because the Cornix cap rejects rather than averages: a set
    # that sits at Ø 493 but p95 813 spends most of its time over the limit, and which
    # trades get refused is then out of our hands. The mean-budget winner is reported
    # alongside, since it is the better choice if occasional rejections are acceptable.
    chosen = max(acts, key=lambda a: sweep[str(a)]["fill_p95_net"])
    chosen_mean = max(acts, key=lambda a: sweep[str(a)]["fill_mean_net"])
    stats, glen = per_act[chosen]
    selections = [per_fill[chosen][m] for m in ("mean", "p95")]

    meta = {
        "task": "T-2026-KYT-9050-042",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "x": args.x,
        "tf": args.tf,
        "lev": args.lev,
        "fee": args.fee,
        "start": args.start,
        "budget": args.budget,
        "activations": acts,
        "chosen_act": chosen,
        "chosen_act_mean_budget": chosen_mean,
        "sweep": sweep,
        "fills_by_act": {str(a): per_fill[a] for a in acts},
        "hold_total_net": round(sum(v["value_hold_net"] for v in stats.values()), 1),
        "hold_occ_mean": round(sum(v["occ_mean_hold"] for v in stats.values()), 1),
        "hold_median_h": round(float(np.median([v["hold_h_median"] for v in stats.values()])), 1),
        "n_trades": len(trades),
        "no_candle": no_candle,
        "legs": stats,
        "selections": selections,
    }
    os.makedirs(args.out, exist_ok=True)
    md = render_md(meta)
    base = os.path.join(args.out, "trailing_slot_budget_live")
    with open(base + ".md", "w", encoding="utf-8") as fh:
        fh.write(md)
    with open(base + ".json", "w", encoding="utf-8") as fh:
        json.dump(
            {**meta, "legs": {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")} for k, v in stats.items()}},
            fh,
            indent=2,
            default=str,
        )
    print(md)
    print(f"→ {base}.md / .json")


if __name__ == "__main__":
    main()
