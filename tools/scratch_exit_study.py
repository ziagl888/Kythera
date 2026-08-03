"""
tools/scratch_exit_study.py — K15 · SRX scratch-reload-exit study (T-2026-CU-9050-137).

Purpose
-----
Checks OFFLINE the practitioner thesis that, for break-&-retest setups (ABR), a
"scratch-reload" exit beats the fixed SL: instead of taking a full 4–12% SL hit,
the position is scratched immediately when a 4h candle closes BACK over the
broken level (`level_price`) (LONG: below it), and reopened on the next
cross + retest of the same level — max. N ∈ {2,4,8} cycles, window
14 days per event. The entry is our existing ABR concept; ONLY the
exit mechanic is new (spec docs/MODEL_CANDIDATES_SPEC_2026-07.md §K15).

None of this goes into a bot: the trade monitor knows neither scratch exits nor
re-entries. Pure falsification replay (Batch-E), read-only.

Event source
------------
Existing ABR1 walkforward replay
`_X/staging_models/replay/abr1_replay_365d.jsonl` (288,281 events, 526 coins).
NO new detector, NO new walkforward run. The baseline (variant a) is
the already simulated first-touch result `net_pnl_pct` of the record — it is
NOT re-simulated (spec requirement). Variant (b)/(c) only replace the
loss side (SL → scratch-reload).

Variants per event
------------------
  (a) Baseline            = record `net_pnl_pct` (untouched, first-touch ladder).
  (b) Scratch-Reload      = scratch at 4h close beyond `level_price`, re-entry
                            on cross+retest, hard SL TOUCH-based as a net.
  (c) like (b), hard SL CLOSE-based — its own grid cell, reported separately.
                            ⚠ Close-based stops underestimate the
                            liquidation risk under leverage (liquidation is touch-based;
                            cross-margin mitigates it, does not eliminate it).
  (aux) TP1-vs-TouchSL    = same geometry as (b), but WITHOUT scratch/reentry
                            (first-touch TP1 against touch SL). For diagnosis only:
                            separates the scratch effect from the TP1-instead-of-ladder
                            effect, because (a) is the original ladder. Not a verdict.

Fees: not reinvented — `walkforward_sim.FEE_PER_SIDE` (0.05%/side →
0.10% round trip), deducted per leg (rule 10).

Survivorship (rule 9): the event population is the ABR1 walkforward over the
coins listed in `coins.json` as TRADABLE TODAY — delisted pairs are missing, so the
loss tail is optimistic. Applies EQUALLY to ALL variants, the
(b)-vs-(a) comparison stays internally consistent.

Operation: BELOW_NORMAL, CPU headroom check, DB strictly read-only (SELECT only),
batched per coin (≈526 coin queries instead of 288k). Result under
`staging_models/scratch_exit_study.{json,md}`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "tools"))

from walkforward_sim import (  # noqa: E402
    FEE_PER_SIDE,
    check_cpu_headroom,
    set_low_priority,
)

from core.candles import read_candles  # noqa: E402
from core.database import db_connection  # noqa: E402

# The replay lives outside the repo in Documents\_X. Absolute path, overridable via --replay.
DEFAULT_REPLAY = r"C:\Users\Michael\Documents\_X\staging_models\replay\abr1_replay_365d.jsonl"

OHLCV_COLUMNS = ("open_time", "open", "high", "low", "close", "volume")
FEE_ROUNDTRIP = 2.0 * FEE_PER_SIDE  # fraction, per leg
WINDOW_DAYS = 14
N_CYCLES = (2, 4, 8)
MAX_CYCLES = max(N_CYCLES)


# ────────────────────────────────────────────────────────────────────────────
# Event stream (lightweight — features are discarded, 378 MB never in RAM)
# ────────────────────────────────────────────────────────────────────────────
def stream_events(path: str, sample_stride: int = 1):
    """Yields (symbol, event_dict) per line. `event` carries only the simulation
    fields — the heavy `features` are discarded. sample_stride>1 takes every
    nth event (documented cap, not silent sampling)."""
    kept = 0
    total = 0
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            total += 1
            if sample_stride > 1 and (total - 1) % sample_stride != 0:
                continue
            r = json.loads(line)
            targets = r.get("targets") or []
            if not targets or r.get("net_pnl_pct") is None:
                continue
            st = r["signal_time"]  # "YYYY-MM-DD HH:MM:SS" naive UTC (writer = UTC instant)
            sig = datetime.strptime(st, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            ev = {
                "dir_long": r["direction"] == "LONG",
                "entry": float(r["entry"]),
                "level": float(r["level_price"]),
                "sl": float(r["sl"]),
                "tp1": float(targets[0]),
                "sig": sig,
                "sig_ns": np.datetime64(sig.replace(tzinfo=None), "ns"),
                "base_net": float(r["net_pnl_pct"]),
                "month": sig.strftime("%Y-%m"),
            }
            kept += 1
            yield r["symbol"], ev
    yield "__STATS__", {"total": total, "kept": kept}


# ────────────────────────────────────────────────────────────────────────────
# Core simulation per event (ONE pass per SL mode, every N derived from it)
# ────────────────────────────────────────────────────────────────────────────
def simulate(o, h, low, c, i0, i1, ev, close_sl):
    """One scratch-reload pass over the 4h candles [i0, i1) (14d window),
    hard cap MAX_CYCLES scratches. Returns the flat net levels after
    each scratch plus the terminal state — `derive()` then derives every
    N ∈ {2,4,8} from this without another pass.

    Returns: (scratch_nets, terminal_outcome, terminal_net, terminal_scratches)
      scratch_nets[k] = cumulative net return (fraction) RIGHT AFTER the
                        (k+1)th scratch (position flat).
    """
    is_long = ev["dir_long"]
    level = ev["level"]
    sl = ev["sl"]
    tp1 = ev["tp1"]

    net = 0.0                    # cumulative, fraction
    pos_entry = ev["entry"]      # current entry price of the open leg
    in_pos = True
    crossed = False              # WAIT_RETEST reached (cross back seen)
    scratch_nets: list[float] = []

    def leg_return(px):
        return (px / pos_entry - 1.0) if is_long else (1.0 - px / pos_entry)

    for i in range(i0, i1):
        oi, hi, li, ci = o[i], h[i], low[i], c[i]  # noqa: F841 (oi unused, for clarity)
        if in_pos:
            # Order on intra-candle ambiguity: hard SL first
            # (pessimistic, like walkforward_sim SL-first), then TP, then scratch.
            if close_sl:
                sl_hit = (ci <= sl) if is_long else (ci >= sl)
                sl_fill = ci  # close-based: fill at the (breached) close
            else:
                sl_hit = (li <= sl) if is_long else (hi >= sl)
                sl_fill = sl  # touch-based: fill at the stop price
            tp_hit = (hi >= tp1) if is_long else (li <= tp1)
            scratch = (ci < level) if is_long else (ci > level)

            if sl_hit:
                net += leg_return(sl_fill) - FEE_ROUNDTRIP
                return scratch_nets, "sl", net, len(scratch_nets)
            if tp_hit:
                net += leg_return(tp1) - FEE_ROUNDTRIP
                return scratch_nets, "tp", net, len(scratch_nets)
            if scratch:
                net += leg_return(ci) - FEE_ROUNDTRIP
                scratch_nets.append(net)
                if len(scratch_nets) >= MAX_CYCLES:
                    return scratch_nets, "exhausted", net, len(scratch_nets)
                in_pos = False
                crossed = False
            # else: hold the position
        else:
            # Wait for the cross back over the level, then retest hold (following
            # candle also closes beyond it) → re-entry at the retest close.
            back = (c[i] >= level) if is_long else (c[i] <= level)
            if not crossed:
                if back:
                    crossed = True
            else:
                if back:
                    pos_entry = c[i]  # re-entry at the confirmed retest close
                    in_pos = True
                    crossed = False
                else:
                    crossed = False  # retest failed → wait for a new cross

    # Window end
    if in_pos:
        net += leg_return(c[i1 - 1]) - FEE_ROUNDTRIP  # forced exit MTM at the last close
        return scratch_nets, "timeout_open", net, len(scratch_nets)
    return scratch_nets, "timeout_flat", net, len(scratch_nets)


def derive(scratch_nets, terminal_outcome, terminal_net, terminal_scr, n_cap):
    """Result for cycle cap n_cap from the ONE simulation pass.

    At cap n_cap, the run stops after the n_cap-th scratch (flat, no re-entry),
    i.e. any terminal resolution that in the uncapped run happened ONLY after ≥ n_cap
    scratches is unreachable for the cap.
    """
    if len(scratch_nets) >= n_cap and terminal_scr >= n_cap:
        return scratch_nets[n_cap - 1], n_cap, "scratch_exhausted"
    return terminal_net, terminal_scr, terminal_outcome


def simulate_geom(o, h, low, c, i0, i1, ev):
    """Aux: pure first-touch TP1-vs-touch-SL geometry (no scratch, no
    re-entry). Isolates the scratch effect from the TP1-instead-of-ladder effect."""
    is_long = ev["dir_long"]
    entry, sl, tp1 = ev["entry"], ev["sl"], ev["tp1"]
    for i in range(i0, i1):
        sl_hit = (low[i] <= sl) if is_long else (h[i] >= sl)
        tp_hit = (h[i] >= tp1) if is_long else (low[i] <= tp1)
        if sl_hit:
            r = (sl / entry - 1.0) if is_long else (1.0 - sl / entry)
            return (r - FEE_ROUNDTRIP) * 100.0
        if tp_hit:
            r = (tp1 / entry - 1.0) if is_long else (1.0 - tp1 / entry)
            return (r - FEE_ROUNDTRIP) * 100.0
    px = c[i1 - 1]
    r = (px / entry - 1.0) if is_long else (1.0 - px / entry)
    return (r - FEE_ROUNDTRIP) * 100.0


# ────────────────────────────────────────────────────────────────────────────
# Aggregation
# ────────────────────────────────────────────────────────────────────────────
class Accum:
    """Collects (net_pct, is_win) plus optional cycles; computes metrics."""

    def __init__(self):
        self.nets: list[float] = []
        self.cycles: list[int] = []

    def add(self, net_pct, cycles=None):
        self.nets.append(net_pct)
        if cycles is not None:
            self.cycles.append(cycles)

    def stats(self):
        if not self.nets:
            return None
        a = np.asarray(self.nets, dtype=float)
        out = {
            "n": int(a.size),
            "wr_pct": round(float((a > 0).mean() * 100.0), 2),
            "avg_net_pct": round(float(a.mean()), 4),
            "median_net_pct": round(float(np.median(a)), 4),
            "p5_net_pct": round(float(np.percentile(a, 5)), 4),
            "p95_net_pct": round(float(np.percentile(a, 95)), 4),
            "sum_net_pct": round(float(a.sum()), 2),
        }
        if self.cycles:
            cyc = np.asarray(self.cycles, dtype=float)
            out["avg_cycles"] = round(float(cyc.mean()), 3)
            out["max_cycles"] = int(cyc.max())
            out["pct_with_reentry"] = round(float((cyc >= 1).mean() * 100.0), 2)
        return out


def main():
    ap = argparse.ArgumentParser(description="K15 SRX scratch-reload-exit study (read-only)")
    ap.add_argument("--replay", default=DEFAULT_REPLAY, help="Path to abr1_replay_*.jsonl")
    ap.add_argument("--limit-symbols", type=int, default=0, help="Only the first N coins (smoke test)")
    ap.add_argument("--sample-stride", type=int, default=1,
                    help="Every nth event (documented cap; 1 = full)")
    ap.add_argument("--out-prefix", default=os.path.join(REPO_ROOT, "staging_models", "scratch_exit_study"))
    ap.add_argument("--skip-cpu-check", action="store_true",
                    help="Skip the hard >90%% CPU abort. Legitimate ONLY because we "
                         "run at BELOW_NORMAL (yields to the fleet) and are the only study job "
                         "running; the VPS is permanently saturated. Explicit + logged instead of silent.")
    args = ap.parse_args()

    set_low_priority()
    if args.skip_cpu_check:
        print("CPU headroom check SKIPPED (--skip-cpu-check) — running at BELOW_NORMAL, "
              "yields to the fleet.")
    else:
        check_cpu_headroom()

    print(f"Reading events from {args.replay} (stride={args.sample_stride}) …")
    by_symbol: dict[str, list[dict]] = defaultdict(list)
    stats = {"total": 0, "kept": 0}
    for sym, ev in stream_events(args.replay, args.sample_stride):
        if sym == "__STATS__":
            stats = ev
            break
        by_symbol[sym].append(ev)
    symbols = sorted(by_symbol)
    if args.limit_symbols:
        symbols = symbols[: args.limit_symbols]
    n_events_used = sum(len(by_symbol[s]) for s in symbols)
    print(f"  {stats['total']} lines, {stats['kept']} usable events, "
          f"{len(by_symbol)} coins. Used: {n_events_used} events / {len(symbols)} coins.")

    # Accumulators
    acc_base = Accum()
    acc_geom = Accum()
    acc = {("b", n): Accum() for n in N_CYCLES}
    acc.update({("c", n): Accum() for n in N_CYCLES})
    # Chrono split & monthly split need (sig, net_a, net_b_perN, net_c_perN)
    rows: list[tuple] = []  # (sig, base, geom, {(v,n):net})
    outcomes = {("b", n): defaultdict(int) for n in N_CYCLES}
    outcomes.update({("c", n): defaultdict(int) for n in N_CYCLES})
    skipped_no_candles = 0

    with db_connection() as conn:
        for si, sym in enumerate(symbols, 1):
            evs = by_symbol[sym]
            smin = min(e["sig"] for e in evs)
            smax = max(e["sig"] for e in evs) + timedelta(days=WINDOW_DAYS)
            try:
                df = read_candles(conn, sym, "4h", start=smin, end=smax,
                                  include_forming=False, columns=OHLCV_COLUMNS)
            except Exception:
                conn.rollback()
                df = None
            if df is None or df.empty:
                skipped_no_candles += len(evs)
                continue
            # open_time is TIMESTAMPTZ; the PG session delivers it in local time (+03).
            # Robustly converted to naive UTC (some reads return object dtype).
            ot_ser = pd.to_datetime(df["open_time"], utc=True)
            ot = ot_ser.dt.tz_localize(None).to_numpy(dtype="datetime64[ns]")
            o = df["open"].to_numpy(dtype=float).tolist()
            hh = df["high"].to_numpy(dtype=float).tolist()
            ll = df["low"].to_numpy(dtype=float).tolist()
            cc = df["close"].to_numpy(dtype=float).tolist()
            win_ns = np.timedelta64(WINDOW_DAYS * 24 * 3600, "s").astype("timedelta64[ns]")

            for ev in evs:
                i0 = int(np.searchsorted(ot, ev["sig_ns"], side="left"))
                i1 = int(np.searchsorted(ot, ev["sig_ns"] + win_ns, side="right"))
                if i1 <= i0:
                    skipped_no_candles += 1
                    continue
                acc_base.add(ev["base_net"])
                acc_geom.add(simulate_geom(o, hh, ll, cc, i0, i1, ev))
                row_nets = {}
                for mode, tag in ((False, "b"), (True, "c")):
                    sn, tout, tnet, tscr = simulate(o, hh, ll, cc, i0, i1, ev, close_sl=mode)
                    for n in N_CYCLES:
                        net_frac, cyc, oc = derive(sn, tout, tnet, tscr, n)
                        net_pct = net_frac * 100.0
                        acc[(tag, n)].add(net_pct, cyc)
                        outcomes[(tag, n)][oc] += 1
                        row_nets[(tag, n)] = net_pct
                rows.append((ev["sig"], ev["base_net"], row_nets, ev["month"]))
            if si % 50 == 0 or si == len(symbols):
                print(f"  [{si}/{len(symbols)}] {sym}: cumulative {len(rows)} events simulated")

    if not rows:
        print("NO events simulated — aborting.")
        sys.exit(1)

    # ── Chrono val/test split (median of signal_time) ──
    rows.sort(key=lambda r: r[0])
    mid = len(rows) // 2
    val, test = rows[:mid], rows[mid:]

    def half_avg(subset, key):
        if key == "base":
            arr = [r[1] for r in subset]
        else:
            arr = [r[2][key] for r in subset if key in r[2]]
        return round(float(np.mean(arr)), 4) if arr else None

    split = {"val_n": len(val), "test_n": len(test),
             "val_cut": val[-1][0].isoformat() if val else None,
             "base": {"val": half_avg(val, "base"), "test": half_avg(test, "base")}}
    for tag in ("b", "c"):
        for n in N_CYCLES:
            split[f"{tag}_N{n}"] = {"val": half_avg(val, (tag, n)), "test": half_avg(test, (tag, n))}

    # ── Verdict: does (b) beat (a) in val AND test? ──
    base_val, base_test = split["base"]["val"], split["base"]["test"]
    verdict_cells = {}
    any_beat = False
    for n in N_CYCLES:
        bv, bt = split[f"b_N{n}"]["val"], split[f"b_N{n}"]["test"]
        beats = (bv is not None and bt is not None
                 and bv > base_val and bt > base_test)
        verdict_cells[f"b_N{n}"] = {
            "val_delta": round(bv - base_val, 4) if bv is not None else None,
            "test_delta": round(bt - base_test, 4) if bt is not None else None,
            "beats_baseline_both_halves": bool(beats),
        }
        any_beat = any_beat or beats
    verdict = "scratch_beats_baseline" if any_beat else "no_op_thesis_falsified"

    # ── Monthly split (avg net per month: base + b_N4 as representative) ──
    months = defaultdict(lambda: {"base": [], "b_N4": [], "c_N4": []})
    for _sig, base, rn, month in rows:
        months[month]["base"].append(base)
        if ("b", 4) in rn:
            months[month]["b_N4"].append(rn[("b", 4)])
            months[month]["c_N4"].append(rn[("c", 4)])
    month_split = {}
    for m in sorted(months):
        d = months[m]
        month_split[m] = {
            "n": len(d["base"]),
            "base_avg": round(float(np.mean(d["base"])), 4),
            "b_N4_avg": round(float(np.mean(d["b_N4"])), 4) if d["b_N4"] else None,
            "c_N4_avg": round(float(np.mean(d["c_N4"])), 4) if d["c_N4"] else None,
        }

    result = {
        "study": "K15 · SRX — Scratch-Reload-Exit (ABR1 events)",
        "task": "T-2026-CU-9050-137",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "replay_source": args.replay,
        "read_only": True,
        "fee_per_side": FEE_PER_SIDE,
        "fee_roundtrip_pct": round(FEE_ROUNDTRIP * 100.0, 4),
        "window_days": WINDOW_DAYS,
        "n_cycles_grid": list(N_CYCLES),
        "sampling": {
            "stride": args.sample_stride,
            "limit_symbols": args.limit_symbols,
            "events_in_file": stats["total"],
            "events_usable": stats["kept"],
            "events_simulated": len(rows),
            "events_skipped_no_candles": skipped_no_candles,
            "symbols_used": len(symbols),
        },
        "verdict": verdict,
        "verdict_cells": verdict_cells,
        "variants": {
            "a_baseline_recorded": acc_base.stats(),
            "aux_geom_tp1_touchsl": acc_geom.stats(),
            **{f"b_scratch_touchSL_N{n}": acc[("b", n)].stats() for n in N_CYCLES},
            **{f"c_scratch_closeSL_N{n}": acc[("c", n)].stats() for n in N_CYCLES},
        },
        "chrono_split": split,
        "outcomes": {f"{t}_N{n}": dict(outcomes[(t, n)]) for t in ("b", "c") for n in N_CYCLES},
        "month_split": month_split,
        "caveats": {
            "close_based_sl": "Variant (c) underestimates the "
            "liquidation risk under leverage — liquidation is touch-based; cross-margin "
            "mitigates it, does not eliminate it.",
            "survivorship": "Event population = ABR1 walkforward over coins "
            "tradable today in coins.json; delisted pairs are missing → loss tail "
            "optimistic (applies equally to all variants, comparison stays internally consistent).",
            "baseline_asymmetry": "(a) is the original ladder (multiple targets); "
            "(b)/(c) use TP1 first-touch. `aux_geom_tp1_touchsl` isolates "
            "the TP1-instead-of-ladder effect from the scratch mechanic.",
            "intra_candle": "When TP+SL occur in the same 4h candle, the SL wins "
            "(pessimistic, like walkforward_sim SL-first).",
            "offline_only": "The trade monitor knows neither scratch exits nor "
            "re-entries — pure offline study, nothing goes into a bot.",
        },
    }

    json_path = args.out_prefix + ".json"
    md_path = args.out_prefix + ".md"
    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, ensure_ascii=False)
    write_md(md_path, result)
    print(f"\nVERDICT: {verdict}")
    print(f"Written: {json_path}\n           {md_path}")


def write_md(path, r):
    v = r["variants"]

    def row(label, s):
        if not s:
            return f"| {label} | – | – | – | – | – | – |"
        return (f"| {label} | {s['n']} | {s['wr_pct']} | {s['avg_net_pct']} | "
                f"{s['median_net_pct']} | {s['p5_net_pct']} | {s['p95_net_pct']} |")

    lines = []
    lines.append(f"# {r['study']}")
    lines.append("")
    lines.append(f"**Task:** {r['task']} · **Generated (UTC):** {r['generated_utc']}")
    lines.append(f"**Source:** `{r['replay_source']}` (read-only)")
    lines.append("")
    s = r["sampling"]
    lines.append(f"**Events:** {s['events_simulated']} simulated "
                 f"(of {s['events_in_file']} in the file, {s['events_usable']} usable; "
                 f"stride={s['stride']}, {s['symbols_used']} coins; "
                 f"{s['events_skipped_no_candles']} skipped without 4h candles).")
    lines.append(f"**Window:** {r['window_days']} days · **Fees:** "
                 f"{r['fee_roundtrip_pct']} % round trip per leg (walkforward_sim.FEE_PER_SIDE).")
    lines.append("")
    lines.append(f"## VERDICT: `{r['verdict']}`")
    lines.append("")
    lines.append("Criterion (spec §K15 / rule 8): variant (b) must beat (a) in "
                 "**avg net PnL in BOTH chrono halves (val AND test)**.")
    lines.append("")
    lines.append("| Cell | Δ val (b–a) | Δ test (b–a) | beats in both? |")
    lines.append("|---|---|---|---|")
    for n in r["n_cycles_grid"]:
        c = r["verdict_cells"][f"b_N{n}"]
        lines.append(f"| b · N={n} | {c['val_delta']} | {c['test_delta']} | "
                     f"{'**YES**' if c['beats_baseline_both_halves'] else 'no'} |")
    lines.append("")
    lines.append("## Metrics per variant (net PnL in % of notional)")
    lines.append("")
    lines.append("| Variant | n | WR % | avg net | median | p5 | p95 |")
    lines.append("|---|---|---|---|---|---|---|")
    lines.append(row("(a) Baseline (record)", v["a_baseline_recorded"]))
    lines.append(row("(aux) TP1-vs-TouchSL", v["aux_geom_tp1_touchsl"]))
    for n in r["n_cycles_grid"]:
        lines.append(row(f"(b) Scratch·TouchSL·N={n}", v[f"b_scratch_touchSL_N{n}"]))
    for n in r["n_cycles_grid"]:
        lines.append(row(f"(c) Scratch·CloseSL·N={n}", v[f"c_scratch_closeSL_N{n}"]))
    lines.append("")
    lines.append("### Cycles / re-entry (scratch variants)")
    lines.append("")
    lines.append("| Cell | avg cycles | max | % with re-entry |")
    lines.append("|---|---|---|---|")
    for t in ("b", "c"):
        for n in r["n_cycles_grid"]:
            st = v[f"{'b_scratch_touchSL' if t == 'b' else 'c_scratch_closeSL'}_N{n}"]
            if st:
                lines.append(f"| {t} · N={n} | {st.get('avg_cycles')} | "
                             f"{st.get('max_cycles')} | {st.get('pct_with_reentry')} |")
    lines.append("")
    sp = r["chrono_split"]
    lines.append("## Chrono split (val = earlier half, test = later)")
    lines.append("")
    lines.append(f"Val n={sp['val_n']} (up to {sp['val_cut']}), test n={sp['test_n']}.")
    lines.append("")
    lines.append("| Cell | avg net val | avg net test |")
    lines.append("|---|---|---|")
    lines.append(f"| (a) Baseline | {sp['base']['val']} | {sp['base']['test']} |")
    for t in ("b", "c"):
        for n in r["n_cycles_grid"]:
            cell = sp[f"{t}_N{n}"]
            lines.append(f"| ({t}) N={n} | {cell['val']} | {cell['test']} |")
    lines.append("")
    lines.append("## Monthly split (avg net, representative N=4)")
    lines.append("")
    lines.append("| Month | n | (a) base | (b) N4 | (c) N4 |")
    lines.append("|---|---|---|---|---|")
    for m, d in r["month_split"].items():
        lines.append(f"| {m} | {d['n']} | {d['base_avg']} | {d['b_N4_avg']} | {d['c_N4_avg']} |")
    lines.append("")
    lines.append("## Caveats")
    lines.append("")
    for k, txt in r["caveats"].items():
        lines.append(f"- **{k}:** {txt}")
    lines.append("")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))


if __name__ == "__main__":
    main()
