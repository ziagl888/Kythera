# SPEC — Stoic 1-2-3 direction module + backtest (T-2026-KYT-9050-024)

## Intent
Translate the discretionary "Stoic Edge System / 1-2-3 Sequence" (Stoic Trader,
@StoicTA) into a **deterministic, lookahead-free** signal generator that emits a
`date,signal` series (`signal in {-1,0,1}`) plug-compatible with the GARCH
validation harness (`tools/research/garch/compare.py --signals`, T-022). This is
a *direction* system (which way) — complementary to the GARCH *sizing* layer
(how much). Then backtest it over a coin sample with an OOS split + a parameter
sensitivity sweep and produce an explicit **Edge / no-Edge** verdict.

The 1-2-3 sequence, operationalized: an impulsive **break** through both MAs
(Step 1), a pullback **retest** that forms a **base** whose boundary is fixed
(Step 2), and a **break of that boundary** on close = **entry** (Step 3). Exit =
the complete *opposite* 1-2-3 (stop-and-reverse) on the declared management
chart.

**Why build (0b verdict = Build):** no existing Kythera strategy encodes this
pattern; it is a new direction concept whose value must be proven by backtest
before any live consideration.

> **Note on the "5 distortions":** the source article names them but its text is
> not in hand. The five guarded below are this task's faithful operationalization
> of the discretionary/lookahead traps the concept warns about (the spec names
> #2 LTF-first-then-HTF and #3 boundary-after-break explicitly). Documented as an
> interpretation, not a transcription.

## Akzeptanzkriterien (binär testbar)

- [ ] AK1: the signal generator is **deterministic** and **lookahead-free** —
  the signal at bar t uses only bars <= t. Proven by prefix-stability:
  `generate_signals(df[:m])` equals the length-m prefix of `generate_signals(df)`.
  — Test: `backtest/test_stoic123_state_machine.py`.
- [ ] AK2: the **Boundary is fixed before the Step-3 breakout bar** — it is
  computed only from base-window bars strictly before Step 3; no post-break bar
  can change it. — Test: prefix-stability + a targeted boundary-invariance test.
- [ ] AK3: each of the **5 distortions** has an explicit guard test that catches
  it (wick-not-close, HTF-invented, boundary-after-break, skipped-retest,
  repaint). — Test: `test_stoic123_state_machine.py` (5 named tests).
- [ ] AK4: `signals.csv` format is compatible with `compare.py --signals` —
  columns `date,signal`, values in `{-1,0,1}` — **verified by running it
  through the harness**. — Test: `backtest/test_stoic123_signals.py` +
  `compare.load_signals` round-trip.
- [ ] AK5: the backtest runs over >= N coins with a **documented OOS split** and
  emits a report with Sharpe / Max-DD / Winrate / Trade-count / Worst-Month +
  a parameter-sensitivity sweep + an explicit **Edge / no-Edge** verdict. —
  verified by `python tools/research/stoic123/backtest.py --coins ... --json`.

## The 5 distortions (guarded)
1. **Wick-not-close** — a break counts only on CLOSE beyond the level by `k·ATR`,
   never a wick/high poke. Guard: `meaningful_break` uses close.
2. **LTF-first-then-HTF-invented** — the HTF location gate is evaluated *as-of*
   the setup bar (last HTF bar with close <= t) and must pass *before* Step 1 is
   accepted; the entry can never retro-fit an HTF context. Guard: HTF gate in
   Step 1, as-of aligned.
3. **Boundary-after-break** — the base boundary is computed only from bars
   strictly before the Step-3 bar. Guard: base window ends before Step 3;
   prefix-stability proves no later bar moves it.
4. **Skipped-retest** — WAIT→Step3 is impossible; Step 2 (retest into a base)
   is mandatory before Step 3. Guard: state machine requires a detected base.
5. **Repaint / future-confirmation** — a once-emitted signal is never revised by
   a later bar. Guard: single causal pass; prefix-stability is the proof.

## Out of Scope
- Live fleet wiring / any bot (research module + backtest only).
- The GARCH sizing comparison is *consumed* here (signals.csv → compare.py) but
  GARCH itself is T-021/-022, not re-implemented.
- A finer-grained management-chart timeframe than the LTF (declared as a
  parameter; the backtest defaults management = LTF for determinism).
- Any deploy / promotion / operator gate.

## Scope of consent
**Erlaubt:** new files under `tools/research/stoic123/**` and
`backtest/test_stoic123_*.py` on branch `feat/t-2026-kyt-9050-024`; CHANGELOG +
KB status in the PR.
**Verboten:** live bot/`core/` code, fleet `requirements.txt`, `.env`/secrets,
model artifacts, `staging_models/`, DB access, `--no-verify`, force-push,
`gh pr merge` (merge-train only).
**Frag zurück:** wiring into a live bot; promoting anything; adding deps to the
fleet lockfile.
