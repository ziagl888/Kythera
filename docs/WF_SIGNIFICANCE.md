# WF significance layer (`tools/wf_significance.py`)

**Purpose:** a replay summary says "+38 R over 365d" — this layer answers
the follow-up question of whether this edge is distinguishable from noise,
before a candidate is discussed toward the live gate. Purely additive on
top of the output of `tools/walkforward_sim.py`, no changes to the
simulator.
(T-2026-CU-9050-027 D3; modeled on HKUDS/Vibe-Trading `backtest/validation.py`
+ `bench_runner_strict.py`, MIT — adapted, not a drop-in.)

## Invocation

```
python tools/wf_significance.py <pfad>/{tag}_replay_{days}d.jsonl \
    [--group-by strategy|strategy+direction] [--n 1000] [--seed 42] \
    [--fee-per-side 0.05] [--min-trades 20] [--out report.json]
```

Input is the trade JSONL from the walk-forward simulator (fields
`strategy`, `direction`, `signal_time`, `outcome_tp1`, `net_pnl_pct`,
`r_multiple`). Output: a console report + `<input>_significance.json`.
Deterministic for a fixed seed (default 42). Replay artifacts live on the
VPS (`Documents\_X\staging_models\replay`) — the run over real Batch-E
outputs is a VPS session; on the build machine, the synthetic tests
(`backtest/test_wf_significance.py`) verify it.

## The three statistics

1. **Random control (sign-flip, the core).** H0: the direction choice has
   no edge — every trade is interchangeable with the counter-trade on the
   same geometry, paying the same fees (`flip(net) = -net - 2*fee_rt`).
   1000 random flip masks yield the null distribution of the mean →
   `p_value` + `random_control_delta_pct`. Deliberately not a test against
   0: the control carries the fee drag of a directionless random trader.
2. **Order permutation for the max drawdown.** Checks whether the loss
   clustering of the observed path is typical of randomness
   (`p_value_dd_worse` = share of permutations with a deeper DD). The vt
   permutation test on Sharpe was deliberately NOT carried over:
   for per-trade % PnL, Sharpe is invariant under order permutation — the
   test would be degenerate. The DD is measured **as an absolute %-point
   figure** below the peak (`equity − peak`), not normalized to the peak
   height — otherwise the random peak height of the fleet-wide multi-coin
   replays confounds the test (T-2026-CU-9050-053, see "finding" below).
   It's a path-clustering statistic, not a real portfolio drawdown
   (concurrent signals stay sequentially chained).
3. **Bootstrap CIs** (resampling with replacement) for per-trade Sharpe
   (deliberately not annualized — trades are not time-regular), `avg_r`
   and TP1 win rate, each with `prob_positive`.

## Reading guide

- `random_control.p_value < 0.05` and `sharpe_per_trade_ci[0] > 0`: the
  edge is distinguishable from randomness — a candidate for the next
  Batch-E stage.
- `p_value_dd_worse` (absolute DD in %-points, since T-2026-CU-9050-053):
  **small** (≲ 0.05) = the losses cluster in the real chronology
  atypically **malignantly** — barely any random order is this bad;
  check the regime dependency and measure the DD risk at the observed
  value. **Close to 1** = almost every order would be equally bad or
  worse, the path was atypically merciful → take the DD budget from
  `simulated_max_dd_median_pp`. The value is a **path-clustering
  statistic in %-points**, not a real portfolio drawdown (concurrent
  signals stay sequentially chained — a limitation, see "finding" below).
  Until the fix, this rule was exactly inverted on the multi-coin
  replays due to the peak normalization.
- **Limitations:** tests ONE candidate. Whoever screens many
  variants additionally needs FDR/deflated Sharpe (deliberately
  non-scope, its own task). No substitute for purge/embargo in the
  simulator itself. And: the sign-flip control assumes `gross' = -gross`
  — a genuinely reversed trade would have been stopped earlier under
  SL-/TP-capped ladder profiles. The control is therefore too negative
  for trend-following-like R:R profiles, **p-values skew too small**:
  don't read past marginal significance as proof. A fairer control (a
  simulate_exit re-run with a mirrored direction) = a separate task.

## First run over real Batch-E outputs (2026-07-10, VPS)

T-2026-CU-9050-040. `--group-by strategy+direction`, `--n 1000`,
`--seed 42`, `--fee-per-side 0.05`; inputs from
`Documents\_X\staging_models\replay`. The run is read-only and
deterministically reproducible (identical report on repetition).
Interpreter: `py -3.13` — the PATH `python` (3.14) has no numpy.

| Candidate | n_closed | mean PnL % | control % | p | Sharpe/trade (95% CI) | avg_r | TP1-WR |
|---|---|---|---|---|---|---|---|
| mis1/LONG | 175.089 | −0,2601 | −0,1000 | 1,000 | [−0,0409, −0,0312] | −0,0463 | 55,9 % |
| mis1/SHORT | 175.027 | +0,0362 | −0,1001 | 0,001 | [+0,0006, +0,0097] | +0,0095 | 56,3 % |
| rub/LONG | 52.081 | −0,3246 | −0,1006 | 1,000 | [−0,0382, −0,0203] | −0,0128 | 60,6 % |
| rub/SHORT | 45.560 | −0,2528 | −0,0996 | 1,000 | [−0,0401, −0,0219] | −0,0269 | 73,9 % |
| abr1/LONG | 77.398 | −0,5480 | −0,0989 | 1,000 | [−0,1156, −0,1008] | −0,0890 | 55,7 % |
| abr1/SHORT | 91.627 | +0,2720 | −0,1002 | 0,001 | [+0,0391, +0,0519] | +0,0445 | 59,2 % |
| ufi1/SHORT | 384 | +17,6594 | −0,0961 | 0,001 | [+0,2726, +0,4867] | +0,3663 | 50,8 % |

**The layer behaves as specified.** Two independent counter-checks: the
control mean hits the round-trip fee drag in all seven groups (−0.0961 …
−0.1006 against an expected −0.10), and the trade-weighted aggregates
from the report reproduce the simulator's `*_summary.json` exactly
(mis1: WR 56.09% / avg_r −0.0184 / avg_pnl −0.1120 against 56.1 /
−0.0184 / −0.112; rub analogous). The p-value agrees with the sign of
the Sharpe CI in every group.

**The replays carry the raw (ROHEN) detector signals, before the model
filter.** The table therefore evaluates the detector, not the deployed
model — no deploy argument in either direction:

- **abr1** matches the live picture: SHORT has a raw edge, LONG is
  significantly worse than a directionless random trader (SHORT runs
  binary @0.75; LONG only as a funding-gated experiment).
- **rub** is raw-negative in BOTH directions, even though
  RUB2-SHORT is deployed live. There, the edge comes from the model
  selection, not from the detector. A significance run over raw signals
  therefore cannot refute a good model.
- **mis1/SHORT** is, despite p = 0.001, practically a null edge (lower
  CI bound 0.0006, avg_r +0.0095). On top of that, the sign-flip control
  biases p downward — exactly the case the limitations note warns
  about.
- **ufi1/SHORT** is the only large raw edge, but it stands on n = 384,
  SHORT-only and a single time window. No reason to touch the park
  decision.

## Finding (fixed, T-2026-CU-9050-053): peak normalization confounded statistic 2

**Diagnosis (state of the first run).** `max_drawdown_pct` normalized
the drawdown to the running peak (`(equity − peak) / peak`). On these
replays the additive equity (`100 + Σ %-PnL`) doesn't support that: per
timestamp there are 8.8 (rub) to 20.2 (mis1) concurrent signals across
530–648 coins, which the path chains as sequential individual bets.
Equity therefore falls far below zero (rub/LONG: 72% of the path
negative, low −35.072) and the ratio ends up mainly measuring **how
high the peak happened to stand**: mis1/SHORT and abr1/SHORT have their
peak at trade 0 (≈95), rub/LONG at 2.477 — hence a visually mild −421%
there against a permutation median of −7.203%. Side finding: the guard
`np.where(peak > 0, peak, 1.0)` silently switched both unit AND scale
when peak ≤ 0 (relative → %-points × 100).

**Fix.** `max_drawdown_pct` now computes the DD **as an absolute
%-point figure** below the peak (`equity − peak`, without
normalization; the +100 base cancels out). The observed and permuted
path are thereby measured exactly the same way, free of the
peak-height artifact; the guard is removed without replacement,
because there's no more division taking place. That makes
`p_value_dd_worse` operationally readable again (reading guide above).
Option chosen: absolute DD instead of an overlap-respecting equity
path — the latter would need capital-allocation/sizing assumptions
that the replay JSONL doesn't carry, and would deviate from the
`sum_net_pnl_pct` reporting convention. **Limitation:** the number
remains a path-clustering statistic, not a real portfolio drawdown
(overlap sequentially chained).

The operative conclusion flips (200 permutations, seed 42,
`--fee-per-side 0,05`; reproduced with the fixed tool):

| Candidate | p_dd_worse before fix (relative) | p_dd_worse after fix (absolute, tool) |
|---|---|---|
| rub/LONG | 1,000 ("atypically merciful") | 0,005 (malignant clustering; obs. −55.208 vs median −17.182) |
| abr1/SHORT | 0,005 | 0,005 |
| ufi1/SHORT | 0,035 | 0,005 (obs. −1.436,72 vs median −278,19) |

For rub/LONG, the old reading rule would have taken the DD budget from
`simulated_max_dd_median_pp`, even though the observed path was worse
than 199 of 200 random orders — now the test displays that correctly.

**No deploy claim from the table above changes.** It rests on
statistic 1 (random-control `p`) and 3 (Sharpe CI), both order-invariant
and untouched by the DD fix; the values above were reproduced
identically with the fixed tool (rub/LONG mean −0,3246, `p`=1,000,
Sharpe negative). The drawdown statistic was already marked "do not
read operationally" and went into no deploy call — it is now merely
usable again.
