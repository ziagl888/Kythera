# tools/wf_significance.py — Signifikanz-Layer über den Walk-Forward-Replay-Output.
#
# Konsumiert das Trade-JSONL von tools/walkforward_sim.py ({tag}_replay_{days}d.jsonl)
# und beantwortet pro Strategie-Kandidat die Frage, die ein Replay-Summary allein
# nicht beantwortet: ist der gemessene Edge von Rauschen unterscheidbar?
#
# Drei Statistiken (Vorbild: HKUDS/Vibe-Trading backtest/validation.py:28-145 und
# src/factors/bench_runner_strict.py:99-195 — Random-Control gegen einen Shuffle
# DERSELBEN Daten statt Test gegen eine abstrakte Null; MIT, T-2026-CU-9050-027 D3):
#
#   1. RANDOM-CONTROL (Sign-Flip-Permutation, der Kern): Unter H0 "die Richtungs-
#      wahl hat keinen Edge" ist jeder Trade austauschbar mit seinem Gegen-Trade
#      auf derselben Geometrie. Der Gegen-Trade zahlt dieselben Fees:
#      flip(net) = -(net + fee_rt) - fee_rt = -net - 2*fee_rt (Näherung: Ladder-
#      Teil-Fills gebühren-gemittelt). n Iterationen zufälliger Flip-Masken
#      liefern die Null-Verteilung des Mittelwerts -> p-Wert + Delta gegen die
#      Kontrolle. Das ist bewusst KEIN Test gegen 0: die Kontrolle trägt den
#      Fee-Drag, den ein richtungsloser Zufalls-Trader real hätte.
#   2. PERMUTATIONSTEST (Trade-Reihenfolge) für MaxDD: Sharpe ist bei per-Trade-
#      %-PnL unter Reihenfolge-Permutation INVARIANT (komponierte Equity:
#      eq_k/eq_{k-1}-1 = pnl_k) — der vt-Permutationstest auf Sharpe wäre hier
#      degeneriert und wird bewusst NICHT übernommen. Pfadabhängig und ehrlich
#      testbar ist der Max-Drawdown der Equity-Kurve in Signalzeit-Reihenfolge.
#   3. BOOTSTRAP-CI (Resampling mit Zurücklegen) für per-Trade-Sharpe, avg_r und
#      Win-Rate. Per-Trade-Sharpe = mean/std der Trade-PnLs, bewusst NICHT
#      annualisiert (Trades sind nicht zeit-regulär; eine sqrt(252)-Skalierung
#      würde Präzision vortäuschen).
#
# Rein additiv: kein Eingriff in walkforward_sim.py. Seed gepinnt (42).
# Läuft DB-frei auf der Build-Maschine; Input ist ein bestehendes Replay-JSONL.
#
# Usage:
#   python tools/wf_significance.py <replay.jsonl> [--group-by strategy|strategy+direction]
#                                   [--n 1000] [--seed 42] [--fee-per-side 0.05]
#                                   [--min-trades 20] [--out <report.json>]
#
# MULTIPLE-TESTING-HINWEIS: dieser Layer testet EINEN Kandidaten. Wer viele
# Kandidaten screent, braucht zusätzlich FDR/Deflated-Sharpe — bewusst nicht
# hier (Non-Scope T-2026-CU-9050-027, eigener Task falls gewünscht).

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

DEFAULT_N = 1000
DEFAULT_SEED = 42
DEFAULT_FEE_PER_SIDE_PCT = 0.05  # Taker je Seite in % (FEE_PER_SIDE walkforward_sim)


# ── Kern-Statistiken ──────────────────────────────────────────────────────────
def max_drawdown_pct(pnls: np.ndarray) -> float:
    """Max-Drawdown (in %, negativ) der additiven Equity-Kurve aus %-PnLs.

    Additiv (Summe der %-PnLs, Basis 100) wie summarize()'s sum_net_pnl_pct —
    konsistent mit dem Replay-Reporting, Reihenfolge-abhängig (das ist der Punkt).
    """
    equity = 100.0 + np.cumsum(pnls)
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / np.where(peak > 0, peak, 1.0)
    return float(dd.min() * 100.0)


def per_trade_sharpe(pnls: np.ndarray) -> float:
    """mean/std der Trade-PnLs — NICHT annualisiert (siehe Modul-Docstring)."""
    std = float(pnls.std())
    return float(pnls.mean() / (std + 1e-12))


def sign_flip_control(pnls: np.ndarray, fee_rt_pct: float, n: int, seed: int) -> dict:
    """Random-Control: zufällige Richtungs-Flips DERSELBEN Trades inkl. Fee-Drag.

    flip(net) = -net - 2*fee_rt (der Gegen-Trade verliert das Brutto und zahlt
    trotzdem den Round-Trip). p_value = Anteil der Kontroll-Mittelwerte >= dem
    beobachteten Mittel (one-sided: "Kandidat besser als richtungsloser Zufall").
    """
    rng = np.random.default_rng(seed)
    observed = float(pnls.mean())
    flipped = -pnls - 2.0 * fee_rt_pct
    control_means = np.empty(n)
    for k in range(n):
        mask = rng.integers(0, 2, size=pnls.size).astype(bool)
        control_means[k] = np.where(mask, pnls, flipped).mean()
    ge = int((control_means >= observed).sum())
    return {
        "observed_mean_pnl_pct": round(observed, 4),
        "control_mean_pnl_pct": round(float(control_means.mean()), 4),
        "random_control_delta_pct": round(observed - float(control_means.mean()), 4),
        "p_value": round((ge + 1) / (n + 1), 4),  # add-one: nie exakt 0 aus MC
        "n_iterations": n,
    }


def order_permutation_test(pnls: np.ndarray, n: int, seed: int) -> dict:
    """Permutationstest der Trade-Reihenfolge -> p-Wert für den Max-Drawdown.

    H0: der beobachtete MaxDD ist nicht milder als der einer zufälligen
    Reihenfolge derselben Trades. p = Anteil Permutationen mit MaxDD <= observed
    (beide negativ; "<=" = tiefer/schlechter). Kleines p => der beobachtete Pfad
    ist untypisch GUTARTIG (Clusterung der Verluste ist nicht zufällig mild).
    """
    rng = np.random.default_rng(seed)
    observed = max_drawdown_pct(pnls)
    sim = np.empty(n)
    for k in range(n):
        sim[k] = max_drawdown_pct(rng.permutation(pnls))
    worse_or_equal = int((sim <= observed).sum())
    return {
        "observed_max_dd_pct": round(observed, 4),
        "simulated_max_dd_median_pct": round(float(np.median(sim)), 4),
        "simulated_max_dd_p5_pct": round(float(np.percentile(sim, 5)), 4),
        "p_value_dd_worse": round((worse_or_equal + 1) / (n + 1), 4),
        "n_permutations": n,
    }


def bootstrap_cis(pnls: np.ndarray, r_vals: np.ndarray, wins: np.ndarray,
                  n: int, seed: int, confidence: float = 0.95) -> dict:
    """Bootstrap-CIs (Resampling mit Zurücklegen) für Sharpe/avg_r/WR."""
    rng = np.random.default_rng(seed)
    alpha = (1.0 - confidence) / 2.0
    sharpes = np.empty(n)
    avg_rs = np.empty(n) if r_vals.size else None
    wrs = np.empty(n)
    m = pnls.size
    for k in range(n):
        idx = rng.integers(0, m, size=m)
        sharpes[k] = per_trade_sharpe(pnls[idx])
        wrs[k] = float(wins[idx].mean())
        if avg_rs is not None:
            ridx = rng.integers(0, r_vals.size, size=r_vals.size)
            avg_rs[k] = float(r_vals[ridx].mean())

    def ci(arr):
        return [round(float(np.percentile(arr, alpha * 100)), 4),
                round(float(np.percentile(arr, (1 - alpha) * 100)), 4)]

    out = {
        "sharpe_per_trade_observed": round(per_trade_sharpe(pnls), 4),
        "sharpe_per_trade_ci": ci(sharpes),
        "sharpe_prob_positive": round(float((sharpes > 0).mean()), 4),
        "win_rate_observed": round(float(wins.mean()), 4),
        "win_rate_ci": ci(wrs),
        "confidence": confidence,
        "n_bootstrap": n,
    }
    if avg_rs is not None:
        out["avg_r_observed"] = round(float(r_vals.mean()), 4)
        out["avg_r_ci"] = ci(avg_rs)
        out["avg_r_prob_positive"] = round(float((avg_rs > 0).mean()), 4)
    return out


# ── Input/Gruppierung ─────────────────────────────────────────────────────────
def load_replay_jsonl(path: str) -> list[dict]:
    trades = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                trades.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # abgebrochene letzte Zeile eines resume-Laufs
    return trades


def group_key(trade: dict, mode: str) -> str:
    strat = str(trade.get("strategy", "unknown"))
    if mode == "strategy+direction":
        return f"{strat}/{trade.get('direction', '?')}"
    return strat


def analyze_group(trades: list[dict], n: int, seed: int, fee_rt_pct: float,
                  min_trades: int) -> dict:
    closed = [t for t in trades if t.get("outcome_tp1") is not None
              and t.get("net_pnl_pct") is not None]
    result: dict = {"n_signals": len(trades), "n_closed": len(closed)}
    if len(closed) < min_trades:
        result["skipped"] = f"n_closed < {min_trades} — zu wenig für belastbare Statistik"
        return result

    # Pfad-Metriken brauchen die reale Reihenfolge -> nach signal_time sortieren.
    closed.sort(key=lambda t: str(t.get("signal_time", "")))
    pnls = np.array([float(t["net_pnl_pct"]) for t in closed])
    r_vals = np.array([float(t["r_multiple"]) for t in closed
                       if t.get("r_multiple") is not None])
    wins = np.array([1.0 if t["outcome_tp1"] == 1 else 0.0 for t in closed])

    if float(pnls.std()) == 0.0:
        result["skipped"] = "PnL-Varianz 0 — Statistik nicht sinnvoll"
        return result

    result["random_control"] = sign_flip_control(pnls, fee_rt_pct, n, seed)
    result["order_permutation"] = order_permutation_test(pnls, n, seed)
    result["bootstrap"] = bootstrap_cis(pnls, r_vals, wins, n, seed)
    result["sum_net_pnl_pct"] = round(float(pnls.sum()), 2)
    return result


def render_report(results: dict) -> str:
    lines = []
    for key, r in results.items():
        lines.append(f"== {key} ==")
        lines.append(f"  Signale: {r['n_signals']}  geschlossen: {r['n_closed']}")
        if "skipped" in r:
            lines.append(f"  SKIP: {r['skipped']}")
            continue
        rc, op, bs = r["random_control"], r["order_permutation"], r["bootstrap"]
        lines.append(
            f"  Random-Control: mean {rc['observed_mean_pnl_pct']:+.4f}% vs Kontrolle "
            f"{rc['control_mean_pnl_pct']:+.4f}% (Delta {rc['random_control_delta_pct']:+.4f}%), "
            f"p={rc['p_value']}"
        )
        lines.append(
            f"  MaxDD-Pfad: beobachtet {op['observed_max_dd_pct']:.2f}% vs Permutations-Median "
            f"{op['simulated_max_dd_median_pct']:.2f}%, p(schlechter)={op['p_value_dd_worse']}"
        )
        sh = bs["sharpe_per_trade_ci"]
        lines.append(
            f"  Sharpe/Trade: {bs['sharpe_per_trade_observed']:.4f} "
            f"[{sh[0]:.4f}, {sh[1]:.4f}] (95% CI), P(>0)={bs['sharpe_prob_positive']}"
        )
        if "avg_r_ci" in bs:
            ar = bs["avg_r_ci"]
            lines.append(
                f"  avg_r: {bs['avg_r_observed']:.4f} [{ar[0]:.4f}, {ar[1]:.4f}], "
                f"P(>0)={bs['avg_r_prob_positive']}"
            )
        wr = bs["win_rate_ci"]
        lines.append(f"  TP1-WR: {bs['win_rate_observed']:.4f} [{wr[0]:.4f}, {wr[1]:.4f}]")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description="Signifikanz-Layer über Walk-Forward-Replay-Output (D3)")
    ap.add_argument("replay_jsonl", help="Pfad zu {tag}_replay_{days}d.jsonl aus walkforward_sim.py")
    ap.add_argument("--group-by", default="strategy", choices=["strategy", "strategy+direction"])
    ap.add_argument("--n", type=int, default=DEFAULT_N, help="MC-/Bootstrap-Iterationen")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--fee-per-side", type=float, default=DEFAULT_FEE_PER_SIDE_PCT,
                    help="Taker-Fee je Seite in %% (für den Fee-Drag der Random-Control)")
    ap.add_argument("--min-trades", type=int, default=20)
    ap.add_argument("--out", default=None, help="Report-JSON (Default: <input>_significance.json)")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    trades = load_replay_jsonl(args.replay_jsonl)
    if not trades:
        raise SystemExit(f"Keine Trades in {args.replay_jsonl}")

    groups: dict[str, list[dict]] = {}
    for t in trades:
        groups.setdefault(group_key(t, args.group_by), []).append(t)

    fee_rt = 2.0 * args.fee_per_side  # Round-Trip in %
    results = {
        key: analyze_group(g, args.n, args.seed, fee_rt, args.min_trades)
        for key, g in sorted(groups.items())
    }

    meta = {
        "input": os.path.abspath(args.replay_jsonl),
        "group_by": args.group_by,
        "n_iterations": args.n,
        "seed": args.seed,
        "fee_roundtrip_pct": fee_rt,
        "note": "Einzel-Kandidaten-Test; Multi-Kandidaten-Screening braucht zusätzlich FDR (Non-Scope D3).",
    }
    out_path = args.out or (os.path.splitext(args.replay_jsonl)[0] + "_significance.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({"meta": meta, "results": results}, fh, indent=2)

    print(render_report(results))
    print(f"\nReport: {out_path}")


if __name__ == "__main__":
    main()
