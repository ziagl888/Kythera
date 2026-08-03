# tools/wf_significance.py — significance layer over walk-forward replay output.
#
# Consumes the trade JSONL from tools/walkforward_sim.py ({tag}_replay_{days}d.jsonl)
# and answers for each strategy candidate the question that a replay summary alone
# cannot answer: is the measured edge distinguishable from noise?
#
# Three statistics (modelled on: HKUDS/Vibe-Trading backtest/validation.py:28-145 and
# src/factors/bench_runner_strict.py:99-195 — random control against a shuffle of the
# SAME data instead of test against an abstract null; MIT, T-2026-CU-9050-027 D3):
#
#   1. RANDOM-CONTROL (sign-flip permutation, the core): under H0 "the direction
#      choice has no edge" each trade is exchangeable with its counter-trade on the
#      same geometry. The counter-trade pays the same fees:
#      flip(net) = -(net + fee_rt) - fee_rt = -net - 2*fee_rt. The fee sum is exactly
#      the round-trip due to linearity of leg_pnl summation; the actual approximation is
#      the symmetry assumption gross' = -gross — a truly reversed trade would have been
#      stopped earlier with SL-/TP-capped ladder profiles. BIAS DIRECTION (Review PR #20):
#      the control is thereby too negative for trend-following-like R:R profiles ->
#      p-values rather too SMALL, marginal significance should not be over-interpreted.
#      Fairer control (simulate_exit re-run with flipped direction) = separate task. n
#      iterations of random flip masks yield the null distribution of the mean -> p-value
#      + delta versus the control. This is deliberately NOT a test against 0: the control
#      carries the fee drag that a directionless random trader would really have.
#   2. PERMUTATION TEST (trade order) for MaxDD: Sharpe is invariant under order
#      permutation for per-trade %-PnL (compounded equity: eq_k/eq_{k-1}-1 = pnl_k) —
#      the vt permutation test on Sharpe would be degenerate here and is deliberately NOT
#      adopted. Path-dependent and honestly testable is the max drawdown of the equity
#      curve in signal chronology. The DD is measured ABSOLUTELY in percentage points
#      below the peak, not normalised to peak height — otherwise the random peak height
#      of fleet-wide multi-coin replays confounds the test (T-2026-CU-9050-053, details
#      in max_drawdown_pct). It is a path-clustering statistic, not true portfolio
#      drawdown (overlap remains sequentially chained).
#   3. BOOTSTRAP CI (resampling with replacement) for per-trade Sharpe, avg_r and
#      win rate. Per-trade Sharpe = mean/std of trade PnLs, deliberately NOT
#      annualised (trades are not time-regular; a sqrt(252) scaling would feign
#      precision).
#
# Purely additive: no intervention in walkforward_sim.py. Seed pinned (42).
# Runs DB-free on the build machine; input is an existing replay JSONL.
#
# Usage:
#   python tools/wf_significance.py <replay.jsonl> [--group-by strategy|strategy+direction]
#                                   [--n 1000] [--seed 42] [--fee-per-side 0.05]
#                                   [--min-trades 20] [--out <report.json>]
#
# MULTIPLE TESTING NOTE: this layer tests ONE candidate. Anyone screening many
# candidates needs additionally FDR/Deflated-Sharpe — deliberately not here (non-scope
# T-2026-CU-9050-027, separate task if desired).

from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np

DEFAULT_N = 1000
DEFAULT_SEED = 42
DEFAULT_FEE_PER_SIDE_PCT = 0.05  # Taker per side in % (FEE_PER_SIDE walkforward_sim)


# ── Core statistics ───────────────────────────────────────────────────────────
def max_drawdown_pct(pnls: np.ndarray) -> float:
    """Max drawdown in percentage POINTS below the running peak of cumulative equity.

    ABSOLUTE (``equity - peak``), NOT normalised to peak height. Cumulative equity
    (Σ %-PnL) is order-dependent (that is the point) and consistent with summarize()'s
    sum_net_pnl_pct.

    Why absolute instead of (equity-peak)/peak (T-2026-CU-9050-053): on fleet-wide
    multi-coin replays the path chains 8–20 simultaneous signals per timestamp as
    sequential single bets, equity thus falls deep below zero. The quotient would
    ultimately have measured the RANDOM peak height (mis1/SHORT + abr1/SHORT peak at
    trade 0 ≈ 95, rub/LONG at 2.477) — the permutation test was thus confounded and
    stated p_dd_worse backwards (rub/LONG "benignly" 1,000 instead of "malignly" 0,005).
    The absolute DD is free from this peak artefact; the +100 basis cancels in (equity -
    peak) and disappears. Side effect: the old division needed an ``np.where(peak > 0,
    peak, 1.0)`` guard that at peak ≤ 0 still switched unit and scaling — this case
    no longer exists here.

    SCOPE: simultaneous signals remain sequentially chained. The number is a
    path-clustering statistic in percentage points, NOT true portfolio drawdown (that
    would need an overlap-respecting equity path with capital allocation — deliberately
    not in this additive layer). For the permutation test (same malignity across all
    orders?) this is the honest size, because observed and permuted path are measured
    exactly the same way.
    """
    equity = np.cumsum(pnls)
    peak = np.maximum.accumulate(equity)
    dd = equity - peak  # <= 0, in Prozentpunkten
    return float(dd.min())


def per_trade_sharpe(pnls: np.ndarray) -> float:
    """mean/std of trade PnLs — NOT annualised (see module docstring)."""
    std = float(pnls.std())
    return float(pnls.mean() / (std + 1e-12))


def sign_flip_control(pnls: np.ndarray, fee_rt_pct: float, n: int, seed: int) -> dict:
    """Random control: random direction flips of the SAME trades incl. fee drag.

    flip(net) = -net - 2*fee_rt (the counter-trade loses the gross and still pays
    round-trip). p_value = proportion of control means >= the observed mean (one-sided:
    "candidate better than directionless chance").
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
        "p_value": round((ge + 1) / (n + 1), 4),  # add-one: never exactly 0 from MC
        "n_iterations": n,
    }


def order_permutation_test(pnls: np.ndarray, n: int, seed: int) -> dict:
    """Permutation test of trade order -> p-value for max drawdown.

    p = proportion of permutations with MaxDD <= observed (both negative; "<=" =
    deeper/worse). SMALL p => hardly any random order is as bad as the observed one —
    losses cluster atypically MALIGNLY in real chronology (check regime dependence).
    p near 1 => almost any order would be equally bad or worse — the observed path was
    atypically benign, budget real DD risk via simulated_max_dd_median_pp rather than
    the observed value. (Direction corrected in review PR #20 — previously was inverted
    here.)
    """
    rng = np.random.default_rng(seed)
    observed = max_drawdown_pct(pnls)
    sim = np.empty(n)
    for k in range(n):
        sim[k] = max_drawdown_pct(rng.permutation(pnls))
    worse_or_equal = int((sim <= observed).sum())
    return {
        "observed_max_dd_pp": round(observed, 4),
        "simulated_max_dd_median_pp": round(float(np.median(sim)), 4),
        "simulated_max_dd_p5_pp": round(float(np.percentile(sim, 5)), 4),
        "p_value_dd_worse": round((worse_or_equal + 1) / (n + 1), 4),
        "n_permutations": n,
    }


def bootstrap_cis(pnls: np.ndarray, r_vals: np.ndarray, wins: np.ndarray,
                  n: int, seed: int, confidence: float = 0.95) -> dict:
    """Bootstrap CIs (resampling with replacement) for Sharpe/avg_r/WR."""
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


# ── Input/Grouping ────────────────────────────────────────────────────────────
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
                continue  # aborted last line of a resume run
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
        result["skipped"] = f"n_closed < {min_trades} — too few for reliable statistics"
        return result

    # Path metrics need real order -> sort by signal_time.
    closed.sort(key=lambda t: str(t.get("signal_time", "")))
    pnls = np.array([float(t["net_pnl_pct"]) for t in closed])
    r_vals = np.array([float(t["r_multiple"]) for t in closed
                       if t.get("r_multiple") is not None])
    wins = np.array([1.0 if t["outcome_tp1"] == 1 else 0.0 for t in closed])

    if float(pnls.std()) == 0.0:
        result["skipped"] = "PnL variance 0 — statistics not meaningful"
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
        lines.append(f"  signals: {r['n_signals']}  closed: {r['n_closed']}")
        if "skipped" in r:
            lines.append(f"  SKIP: {r['skipped']}")
            continue
        rc, op, bs = r["random_control"], r["order_permutation"], r["bootstrap"]
        lines.append(
            f"  Random control: mean {rc['observed_mean_pnl_pct']:+.4f}% vs control "
            f"{rc['control_mean_pnl_pct']:+.4f}% (delta {rc['random_control_delta_pct']:+.4f}%), "
            f"p={rc['p_value']}"
        )
        lines.append(
            f"  MaxDD path (abs, %pts): observed {op['observed_max_dd_pp']:.2f} vs "
            f"permutation median {op['simulated_max_dd_median_pp']:.2f}, "
            f"p(worse)={op['p_value_dd_worse']}"
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
    ap = argparse.ArgumentParser(description="Significance layer over walk-forward replay output (D3)")
    ap.add_argument("replay_jsonl", help="Path to {tag}_replay_{days}d.jsonl from walkforward_sim.py")
    ap.add_argument("--group-by", default="strategy", choices=["strategy", "strategy+direction"])
    ap.add_argument("--n", type=int, default=DEFAULT_N, help="MC/bootstrap iterations")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--fee-per-side", type=float, default=DEFAULT_FEE_PER_SIDE_PCT,
                    help="Taker fee per side in %% (for fee drag of random control)")
    ap.add_argument("--min-trades", type=int, default=20)
    ap.add_argument("--out", default=None, help="Report JSON (default: <input>_significance.json)")
    args = ap.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    trades = load_replay_jsonl(args.replay_jsonl)
    if not trades:
        raise SystemExit(f"No trades in {args.replay_jsonl}")

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
        # Unit explicit in report: the MaxDD is absolute in percentage points (suffix
        # _pp), not peak-normalised. Old reports carry _pct and are NOT comparable
        # (T-2026-CU-9050-053).
        "max_dd_unit": "percentage_points",
        "note": "Single-candidate test; multi-candidate screening additionally needs FDR (non-scope D3).",
    }
    out_path = args.out or (os.path.splitext(args.replay_jsonl)[0] + "_significance.json")
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump({"meta": meta, "results": results}, fh, indent=2)

    print(render_report(results))
    print(f"\nReport: {out_path}")


if __name__ == "__main__":
    main()
