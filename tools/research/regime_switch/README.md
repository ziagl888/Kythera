# regime_switch — probabilistic / soft regime weighting vs. the live rule

Self-contained, DB-free research study (T-2026-KYT-9050-029) prompted by the
"HMM regime-adaptive strategy" thread. Does a probabilistic (HMM) or soft
(confidence-weighted) regime timeline reduce **whipsaw** and the **TREND-hold
defect** vs. Kythera's live rule (debounce + §22 hysteresis) — *without* losing
regime **separation**? Full rationale and fidelity contract: [`SPEC.md`](SPEC.md).

Like `tools/research/garch` and `tools/research/stoic123`, this is **NO-EDGE-
tolerant**: the honest answer may be "the soft/HMM timeline is smoother but no
better separated" — that is a result, not a failure.

## What it does

1. Fetches BTC + BTCDOM 15m klines off ccxt (`binanceusdm`) — no DB, no creds.
2. Reconstructs the `core.regime_logic` classifier features over the full history
   (causal; the real `classify_*` are imported, not rebuilt — Hard Rule 7).
3. Builds four regime timelines: **RAW** (no damping) · **RULE** (live baseline) ·
   **HMM** (3-state Gaussian, causal forward-filter) · **SOFT** (EMA-smoothed
   confidence).
4. Scores whipsaw / TREND-hold / eta² separation (1h/4h/24h) on the common window.
5. Prints a table + an explicit **EDGE / NO-EDGE** verdict; writes the full JSON
   to `staging_models/replay/regime_switch_study.json`.

## Run

```bash
pip install -r tools/research/regime_switch/requirements-regime.txt

# full run (fetches once, caches klines for offline reruns)
python -m tools.research.regime_switch.study --days 365 --csv-dir /tmp/regime_klines

# fast smoke (no HMM)
python -m tools.research.regime_switch.study --days 365 --no-hmm --csv-dir /tmp/regime_klines

# DB-free tests (no network) — pins the debounce port to the live source
python backtest/test_regime_switch_study.py
```

## The boundary (read before acting on this)

This measures **regime-timeline quality and BTC-conditional separation only**. It
does **not** measure the PnL effect on real bot forwards — that is DB-bound and
lives in `tools/rom1_counterfactual.py` (VPS session). Treat a positive result
here as a *gate* to spend VPS time on the DB counterfactual, not as a live signal.
Any change to ROM1 (Bot 28) is Michi-gated (orchestrator gating = escalation).
