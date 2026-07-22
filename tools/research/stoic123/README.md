# Stoic 1-2-3 — Kythera direction module + backtest

A deterministic, **lookahead-free** translation of the discretionary "Stoic Edge
System / 1-2-3 Sequence" (Stoic Trader, @StoicTA) into a signal generator, plus a
multi-timeframe backtest with an OOS split and an Edge / no-Edge verdict
(`T-2026-KYT-9050-024`).

> **Direction, not sizing.** This says *which way*; the GARCH module
> (`tools/research/garch/`, T-021/-022) says *how much*. The two compose:
> `order_size = signal × size_multiplier`. Its `signals.csv` feeds straight into
> `compare.py --signals` to measure the 1-2-3 edge and whether GARCH sizing lifts
> it, in one run.

## The 1-2-3 sequence (operationalized)

```
WAIT ── Step 1 ──▶ STEP1 ── Step 2 ──▶ STEP2 ── Step 3 ──▶ ENTRY
       break of              retest forms          close breaks the
       both MAs by           a base; its           fixed Boundary by
       k·ATR (close),        Boundary is           k·ATR  (t > base_end)
       HTF gate ok           FIXED here
```
Exit = the complete opposite 1-2-3 (stop-and-reverse). Management chart = LTF by
default (declared as a parameter). Every fuzzy knob lives in `params.py`.

### The 5 distortions (guarded + tested)
1. **Wick-not-close** — breaks are close-based with a k·ATR margin.
2. **LTF-first / HTF-invented** — the HTF location gate is checked *as-of* the
   setup bar, before Step 1.
3. **Boundary-after-break** — the Boundary is set at Step 2 from bars before the
   Step-3 bar; proven fixed by prefix-stability.
4. **Skipped-retest** — Step 2 (a detected base) is mandatory; WAIT→Step 3 is impossible.
5. **Repaint** — one causal pass; a set signal is never revised (prefix-stable).

> The source article names the five distortions but its text was not in hand;
> the five guarded here are this module's faithful operationalization (SPEC.md).

## Library use

```python
from tools.research.stoic123 import generate_signals, StoicParams, write_signals_csv

pos = generate_signals(ltf_ohlc, htf_ohlc, StoicParams())   # -1/0/1 per bar
write_signals_csv(ltf_ohlc, htf_ohlc, "signals.csv")        # date,signal for compare.py
```

## CLI

```
# multi-timeframe backtest: OOS split + sensitivity sweep + Edge/no-Edge verdict
python tools/research/stoic123/backtest.py --coins BTC/USDT,ETH/USDT --ltf 4h --htf 1d
python tools/research/stoic123/backtest.py --coins BTC/USDT --with-garch --json
```

`--with-garch` runs the chosen signals through `tools/research/garch/compare.py`
(fixed vs vol-targeted). `arch`/`ccxt` come from
`tools/research/garch/requirements-garch.txt` (not the fleet lockfile).

## Verdict gate

Per coin: fit the parameter sweep on the in-sample split, judge the best combo on
the out-of-sample split. **EDGE** iff OOS Sharpe ≥ 0.30 with positive expectancy
and ≥ 10 trades; **NO-EDGE** / **INSUFFICIENT** otherwise. This is a research
verdict — it deploys nothing (any live use is a separate, operator-gated task).

## Tests (DB-free, no arch/ccxt)

```
python backtest/test_stoic123_rules.py          # Phase-1 primitives
python backtest/test_stoic123_state_machine.py  # 5 distortions + prefix-stability
python backtest/test_stoic123_signals.py        # signals.csv ⟷ compare.py
python backtest/test_stoic123_backtest.py        # trade stats, OOS, verdict
```
