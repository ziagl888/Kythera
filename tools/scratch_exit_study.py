"""
tools/scratch_exit_study.py — K15 · SRX Scratch-Reload-Exit-Studie (T-2026-CU-9050-137).

Zweck
-----
Prüft OFFLINE die Praktiker-These, dass bei Break-&-Retest-Setups (ABR) ein
"Scratch-Reload"-Exit den fixen SL schlägt: Statt einen vollen 4–12 %-SL-Hit zu
nehmen, wird die Position sofort gescratcht, wenn eine 4h-Kerze ZURÜCK über das
gebrochene Level (`level_price`) schließt (LONG: darunter), und beim nächsten
Cross + Retest desselben Levels neu eröffnet — max. N ∈ {2,4,8} Zyklen, Fenster
14 Tage je Event. Der Entry ist unser bestehendes ABR-Konzept; NUR die
Exit-Mechanik ist neu (Spec docs/MODEL_CANDIDATES_SPEC_2026-07.md §K15).

Nichts davon geht in einen Bot: der Trade-Monitor kennt weder Scratch-Exits noch
Re-Entries. Reiner Falsifikations-Replay (Batch-E), read-only.

Event-Quelle
------------
Vorhandener ABR1-Walkforward-Replay
`_X/staging_models/replay/abr1_replay_365d.jsonl` (288.281 Events, 526 Coins).
KEIN neuer Detektor, KEIN neuer Walkforward-Lauf. Die Baseline (Variante a) ist
das bereits simulierte First-Touch-Ergebnis `net_pnl_pct` des Records — es wird
NICHT neu simuliert (Spec-Vorgabe). Variante (b)/(c) ersetzen nur die
Verlust-Seite (SL → Scratch-Reload).

Varianten je Event
------------------
  (a) Baseline           = Record-`net_pnl_pct` (ungetouched, First-Touch-Ladder).
  (b) Scratch-Reload      = Scratch bei 4h-Close jenseits `level_price`, Re-Entry
                            bei Cross+Retest, harter SL TOUCH-basiert als Netz.
  (c) wie (b), harter SL CLOSE-basiert — eigene Grid-Zelle, getrennt ausgewiesen.
                            ⚠ Close-basierte Stops unterschätzen bei Hebel das
                            Liquidationsrisiko (Liquidation ist Touch-basiert;
                            Cross-Margin mildert, eliminiert es nicht).
  (aux) TP1-vs-TouchSL    = dieselbe Geometrie wie (b), aber OHNE Scratch/Reentry
                            (First-Touch TP1 gegen Touch-SL). Nur zur Diagnose:
                            trennt den Scratch-Effekt vom TP1-statt-Ladder-Effekt,
                            weil (a) die Original-Ladder ist. Nicht wertend.

Fees: nicht neu erfunden — `walkforward_sim.FEE_PER_SIDE` (0,05 %/Seite →
0,10 % Round-Trip), je Leg abgezogen (Regel 10).

Survivorship (Regel 9): die Event-Population ist der ABR1-Walkforward über die
in `coins.json` gelisteten, HEUTE handelbaren Coins — delistete Paare fehlen, der
Verlust-Tail ist damit optimistisch. Gilt für ALLE Varianten gleichermaßen, der
(b)-vs-(a)-Vergleich bleibt intern konsistent.

Betrieb: BELOW_NORMAL, CPU-Headroom-Check, DB strikt read-only (nur SELECT),
Batch je Coin (≈526 Coin-Queries statt 288k). Ergebnis nach
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

# Der Replay liegt außerhalb des Repos in Documents\_X. Absoluter Pfad, per --replay überschreibbar.
DEFAULT_REPLAY = r"C:\Users\Michael\Documents\_X\staging_models\replay\abr1_replay_365d.jsonl"

OHLCV_COLUMNS = ("open_time", "open", "high", "low", "close", "volume")
FEE_ROUNDTRIP = 2.0 * FEE_PER_SIDE  # Fraktion, je Leg
WINDOW_DAYS = 14
N_CYCLES = (2, 4, 8)
MAX_CYCLES = max(N_CYCLES)


# ────────────────────────────────────────────────────────────────────────────
# Event-Stream (leichtgewichtig — Features werden verworfen, 378 MB nie im RAM)
# ────────────────────────────────────────────────────────────────────────────
def stream_events(path: str, sample_stride: int = 1):
    """Yields (symbol, event_dict) je Zeile. `event` trägt nur die Simulations-
    Felder — die schweren `features` werden verworfen. sample_stride>1 nimmt jedes
    n-te Event (dokumentierter Cap, kein Silent-Sampling)."""
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
            st = r["signal_time"]  # "YYYY-MM-DD HH:MM:SS" naive-UTC (Writer = UTC-Instant)
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
# Kern-Simulation je Event (EIN Durchlauf je SL-Modus, alle N daraus abgeleitet)
# ────────────────────────────────────────────────────────────────────────────
def simulate(o, h, low, c, i0, i1, ev, close_sl):
    """Ein Scratch-Reload-Durchlauf über die 4h-Kerzen [i0, i1) (14d-Fenster),
    harte Obergrenze MAX_CYCLES Scratches. Liefert die Flat-Netto-Stände nach
    jedem Scratch plus den Terminal-Zustand — daraus leitet `derive()` jedes
    N ∈ {2,4,8} ohne erneuten Durchlauf ab.

    Rückgabe: (scratch_nets, terminal_outcome, terminal_net, terminal_scratches)
      scratch_nets[k] = kumulierter Netto-Ertrag (Fraktion) DIREKT nach dem
                        (k+1)-ten Scratch (Position flat).
    """
    is_long = ev["dir_long"]
    level = ev["level"]
    sl = ev["sl"]
    tp1 = ev["tp1"]

    net = 0.0                    # kumuliert, Fraktion
    pos_entry = ev["entry"]      # aktueller Einstiegspreis des offenen Legs
    in_pos = True
    crossed = False              # WAIT_RETEST erreicht (Cross zurück gesehen)
    scratch_nets: list[float] = []

    def leg_return(px):
        return (px / pos_entry - 1.0) if is_long else (1.0 - px / pos_entry)

    for i in range(i0, i1):
        oi, hi, li, ci = o[i], h[i], low[i], c[i]  # noqa: F841 (oi ungenutzt, Klarheit)
        if in_pos:
            # Reihenfolge bei Intra-Kerzen-Ambiguität: harter SL zuerst
            # (pessimistisch, wie walkforward_sim SL-first), dann TP, dann Scratch.
            if close_sl:
                sl_hit = (ci <= sl) if is_long else (ci >= sl)
                sl_fill = ci  # Close-basiert: Fill am (durchgeschlossenen) Close
            else:
                sl_hit = (li <= sl) if is_long else (hi >= sl)
                sl_fill = sl  # Touch-basiert: Fill am Stop-Preis
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
            # sonst: Position halten
        else:
            # Warte auf Cross zurück über das Level, dann Retest-Halt (Folgekerze
            # schließt ebenfalls jenseits) → Re-Entry am Retest-Close.
            back = (c[i] >= level) if is_long else (c[i] <= level)
            if not crossed:
                if back:
                    crossed = True
            else:
                if back:
                    pos_entry = c[i]  # Re-Entry am bestätigten Retest-Close
                    in_pos = True
                    crossed = False
                else:
                    crossed = False  # Retest gescheitert → auf neuen Cross warten

    # Fenster-Ende
    if in_pos:
        net += leg_return(c[i1 - 1]) - FEE_ROUNDTRIP  # Zwangs-Exit MTM am letzten Close
        return scratch_nets, "timeout_open", net, len(scratch_nets)
    return scratch_nets, "timeout_flat", net, len(scratch_nets)


def derive(scratch_nets, terminal_outcome, terminal_net, terminal_scr, n_cap):
    """Ergebnis für Zyklen-Cap n_cap aus dem EINEN Simulationslauf.

    Bei Cap n_cap wird nach dem n_cap-ten Scratch gestoppt (flat, kein Re-Entry),
    d.h. jede Terminal-Auflösung, die im ungekappten Lauf ERST nach ≥ n_cap
    Scratches kam, ist für den Cap unerreichbar.
    """
    if len(scratch_nets) >= n_cap and terminal_scr >= n_cap:
        return scratch_nets[n_cap - 1], n_cap, "scratch_exhausted"
    return terminal_net, terminal_scr, terminal_outcome


def simulate_geom(o, h, low, c, i0, i1, ev):
    """Aux: reine First-Touch TP1-vs-Touch-SL-Geometrie (kein Scratch, kein
    Re-Entry). Isoliert den Scratch-Effekt vom TP1-statt-Ladder-Effekt."""
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
    """Sammelt (net_pct, is_win) plus optional Zyklen; berechnet Kennzahlen."""

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
    ap.add_argument("--replay", default=DEFAULT_REPLAY, help="Pfad zum abr1_replay_*.jsonl")
    ap.add_argument("--limit-symbols", type=int, default=0, help="Nur die ersten N Coins (Smoke)")
    ap.add_argument("--sample-stride", type=int, default=1,
                    help="Jedes n-te Event (dokumentierter Cap; 1 = voll)")
    ap.add_argument("--out-prefix", default=os.path.join(REPO_ROOT, "staging_models", "scratch_exit_study"))
    ap.add_argument("--skip-cpu-check", action="store_true",
                    help="Den harten >90%%-CPU-Abbruch überspringen. Legitim NUR weil wir "
                         "BELOW_NORMAL laufen (yieldet an die Fleet) und der einzige Study-Job "
                         "sind; der VPS ist dauer-saturiert. Explizit + geloggt statt still.")
    args = ap.parse_args()

    set_low_priority()
    if args.skip_cpu_check:
        print("CPU-Headroom-Check ÜBERSPRUNGEN (--skip-cpu-check) — Lauf bei BELOW_NORMAL, "
              "yieldet an die Fleet.")
    else:
        check_cpu_headroom()

    print(f"Lese Events aus {args.replay} (stride={args.sample_stride}) …")
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
    print(f"  {stats['total']} Zeilen, {stats['kept']} verwendbare Events, "
          f"{len(by_symbol)} Coins. Genutzt: {n_events_used} Events / {len(symbols)} Coins.")

    # Akkumulatoren
    acc_base = Accum()
    acc_geom = Accum()
    acc = {("b", n): Accum() for n in N_CYCLES}
    acc.update({("c", n): Accum() for n in N_CYCLES})
    # Chrono-Split & Monats-Split brauchen (sig, net_a, net_b_perN, net_c_perN)
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
            # open_time ist TIMESTAMPTZ; die PG-Session liefert es in Ortszeit (+03).
            # Robust nach UTC-naiv (manche Reads geben object-dtype zurück).
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
                print(f"  [{si}/{len(symbols)}] {sym}: kumuliert {len(rows)} Events simuliert")

    if not rows:
        print("KEINE Events simuliert — Abbruch.")
        sys.exit(1)

    # ── Chrono val/test-Split (Median der signal_time) ──
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

    # ── Verdict: (b) schlägt (a) in Val UND Test? ──
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

    # ── Monats-Split (avg net je Monat: base + b_N4 als Repräsentant) ──
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
        "study": "K15 · SRX — Scratch-Reload-Exit (ABR1-Events)",
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
            "close_based_sl": "Variante (c) unterschätzt bei Hebel das "
            "Liquidationsrisiko — Liquidation ist Touch-basiert; Cross-Margin "
            "mildert, eliminiert das nicht.",
            "survivorship": "Event-Population = ABR1-Walkforward über heute in "
            "coins.json handelbare Coins; delistete Paare fehlen → Verlust-Tail "
            "optimistisch (gilt für alle Varianten gleich, Vergleich intern konsistent).",
            "baseline_asymmetry": "(a) ist die Original-Ladder (mehrere Targets); "
            "(b)/(c) verwenden TP1-First-Touch. `aux_geom_tp1_touchsl` isoliert "
            "den TP1-statt-Ladder-Effekt von der Scratch-Mechanik.",
            "intra_candle": "Bei TP+SL in derselben 4h-Kerze gewinnt der SL "
            "(pessimistisch, wie walkforward_sim SL-first).",
            "offline_only": "Der Trade-Monitor kennt weder Scratch-Exits noch "
            "Re-Entries — reine Offline-Studie, nichts geht in einen Bot.",
        },
    }

    json_path = args.out_prefix + ".json"
    md_path = args.out_prefix + ".md"
    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, ensure_ascii=False)
    write_md(md_path, result)
    print(f"\nVERDICT: {verdict}")
    print(f"Geschrieben: {json_path}\n           {md_path}")


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
    lines.append(f"**Task:** {r['task']} · **Generiert (UTC):** {r['generated_utc']}")
    lines.append(f"**Quelle:** `{r['replay_source']}` (read-only)")
    lines.append("")
    s = r["sampling"]
    lines.append(f"**Events:** {s['events_simulated']} simuliert "
                 f"(von {s['events_in_file']} im File, {s['events_usable']} verwendbar; "
                 f"stride={s['stride']}, {s['symbols_used']} Coins; "
                 f"{s['events_skipped_no_candles']} ohne 4h-Kerzen übersprungen).")
    lines.append(f"**Fenster:** {r['window_days']} Tage · **Fees:** "
                 f"{r['fee_roundtrip_pct']} % Round-Trip je Leg (walkforward_sim.FEE_PER_SIDE).")
    lines.append("")
    lines.append(f"## VERDICT: `{r['verdict']}`")
    lines.append("")
    lines.append("Kriterium (Spec §K15 / Regel 8): Variante (b) muss (a) im "
                 "**Ø-Netto-PnL in BEIDEN Chrono-Hälften (Val UND Test)** schlagen.")
    lines.append("")
    lines.append("| Zelle | Δ Val (b–a) | Δ Test (b–a) | schlägt in beiden? |")
    lines.append("|---|---|---|---|")
    for n in r["n_cycles_grid"]:
        c = r["verdict_cells"][f"b_N{n}"]
        lines.append(f"| b · N={n} | {c['val_delta']} | {c['test_delta']} | "
                     f"{'**JA**' if c['beats_baseline_both_halves'] else 'nein'} |")
    lines.append("")
    lines.append("## Kennzahlen je Variante (Netto-PnL in % des Nominals)")
    lines.append("")
    lines.append("| Variante | n | WR % | Ø net | Median | p5 | p95 |")
    lines.append("|---|---|---|---|---|---|---|")
    lines.append(row("(a) Baseline (Record)", v["a_baseline_recorded"]))
    lines.append(row("(aux) TP1-vs-TouchSL", v["aux_geom_tp1_touchsl"]))
    for n in r["n_cycles_grid"]:
        lines.append(row(f"(b) Scratch·TouchSL·N={n}", v[f"b_scratch_touchSL_N{n}"]))
    for n in r["n_cycles_grid"]:
        lines.append(row(f"(c) Scratch·CloseSL·N={n}", v[f"c_scratch_closeSL_N{n}"]))
    lines.append("")
    lines.append("### Zyklen / Re-Entry (Scratch-Varianten)")
    lines.append("")
    lines.append("| Zelle | Ø Zyklen | max | % mit Re-Entry |")
    lines.append("|---|---|---|---|")
    for t in ("b", "c"):
        for n in r["n_cycles_grid"]:
            st = v[f"{'b_scratch_touchSL' if t == 'b' else 'c_scratch_closeSL'}_N{n}"]
            if st:
                lines.append(f"| {t} · N={n} | {st.get('avg_cycles')} | "
                             f"{st.get('max_cycles')} | {st.get('pct_with_reentry')} |")
    lines.append("")
    sp = r["chrono_split"]
    lines.append("## Chrono-Split (Val = frühere Hälfte, Test = spätere)")
    lines.append("")
    lines.append(f"Val n={sp['val_n']} (bis {sp['val_cut']}), Test n={sp['test_n']}.")
    lines.append("")
    lines.append("| Zelle | Ø net Val | Ø net Test |")
    lines.append("|---|---|---|")
    lines.append(f"| (a) Baseline | {sp['base']['val']} | {sp['base']['test']} |")
    for t in ("b", "c"):
        for n in r["n_cycles_grid"]:
            cell = sp[f"{t}_N{n}"]
            lines.append(f"| ({t}) N={n} | {cell['val']} | {cell['test']} |")
    lines.append("")
    lines.append("## Monats-Split (Ø net, Repräsentant N=4)")
    lines.append("")
    lines.append("| Monat | n | (a) base | (b) N4 | (c) N4 |")
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
