# regime_switch — SPEC (T-2026-KYT-9050-029)

## Question

The HMM-regime thread (Venus/@RitOnchain) claims a probabilistic 3-state regime
model with **soft switching** cuts turnover 40–60% and lifts risk-adjusted return.
Kythera's regime orchestrator (ROM1, Bot 28) is a **rule-based** re-forwarder
(T-122, no ML) whose known defects are: whitelist 89% default-open, *detector
never holds TREND*, auto-close cuts winners; the classifier already carries a
**debounce (2/3 checks) + §22 hysteresis** whipsaw damper.

> Does a probabilistic / soft regime weighting reduce whipsaw and the TREND-hold
> defect vs. the live rule — **without degrading regime separation**?

This is answerable **DB-free**: the classifiers in `core/regime_logic.py` are pure,
and BTC/BTCDOM 15m klines come off ccxt (no credentials, Hard Rule 1).

## Method

Four regime timelines over one identical, causally-reconstructed feature frame:

| id | variant | definition |
|----|---------|------------|
| A | **RAW**  | `classify_btc_regime`, `prev_regime=None` — no hysteresis, no debounce. Whipsaw upper bound. |
| B | **RULE** | The faithful live loop: 5-min cadence (`CHECKS_PER_CANDLE=3`, features piecewise-constant per 15m candle), §22 hysteresis fed back into classification, then the debounce state machine. **Live baseline.** |
| C | **HMM**  | 3-state `GaussianHMM(ret_4h, atr_4h_pct)`, walk-forward refit, **causal forward-filter** decode (no intra-block look-ahead). BULL/NEUTRAL/BEAR. |
| D | **SOFT** | "Soft switching" on our own classifier: EMA-smoothed per-regime confidence vector, effective = argmax. Continuous analog of debounce, no ML. |

### Metrics (on the common window, past all warmups — apples-to-apples)

* **Whipsaw**: switches/30d, mean/median dwell (h), % episodes < 1h.
* **TREND-hold defect**: % time in directional states, mean/median TREND episode
  length, count + % of TREND episodes < 1h (the `regime_logic` "34% < 1h" figure).
* **Separation**: per-state forward-return + **eta²** (between-state share of
  forward-return variance) at **1h / 4h(primary) / 24h** horizons. Vocabulary-
  agnostic → comparable across the 5-regime and BULL/NEUTRAL/BEAR alphabets.

### Verdict

**EDGE** iff a candidate (HMM or SOFT) beats RULE on whipsaw **AND** holds-or-
improves eta² (≥98% of baseline) **AND** does not worsen the TREND-episode <1h
share by >5pp. Otherwise **NO-EDGE**.

## Fidelity & honesty boundaries

1. **Shared classifiers, not rebuilt** (Hard Rule 7): `classify_*` are imported
   from `core.regime_logic`. Only `compute_features` (DB read) and the debounce
   state (DB persist) are ported — `features.py` and `timelines._step_debounce`.
   The port is pinned to the source by `test_regime_switch_study.py` (a fake
   single-row `regime_current` conn drives the *real* `apply_debounce` and the
   port must match its effective-regime stream).
2. **Reconstruction, not a byte-copy** of the DB `regime_history`: ccxt klines +
   a trailing rolling percentile (vs. the DB path's fresh 31-day read window).
   Immaterial to a *comparative* study — all four variants share the features.
3. **Causal throughout**: pct_change/ewm/rolling are backward-only; the HMM uses
   a forward filter, not Viterbi/forward-backward smoothing. No look-ahead.
4. **Timeline + separation only.** The PnL effect on real bot forwards is
   **DB-bound** — `tools/rom1_counterfactual.py` on the VPS. This study is the
   necessary precursor: if a variant doesn't separate the timeline better, the
   DB counterfactual is not worth the VPS time.
5. Overlapping H>1 forward returns keep eta² valid (descriptive variance-share,
   shared scheme) but make per-state Sharpe optimistic — read Sharpe as ordinal.

## Files

`ccxt_data.py` (klines) · `features.py` (feature reconstruction) ·
`timelines.py` (A/B/C/D) · `metrics.py` (whipsaw/trend-hold/separation) ·
`study.py` (driver + verdict). Tests: `backtest/test_regime_switch_study.py`.

## Run

```
pip install -r tools/research/regime_switch/requirements-regime.txt
python -m tools.research.regime_switch.study --days 365 --csv-dir <cache>
python backtest/test_regime_switch_study.py    # DB-free, no network
```
