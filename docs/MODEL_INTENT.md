# Model Intent Register — the original idea per model

**Purpose:** Before any further training, fixing or deploying, we pin down the
ORIGINAL idea (intended) for each model, compare it with what the pipeline/bot
actually do today (actual), and flag drift. Rule from now on: **No retrain and no
deploy whose label does not demonstrably answer the intended question.**

Trigger: during the MIS1 retrain (Batch E / 2026-07-05) the label was silently
switched from "±X% move within T" (operator concept) to "TP1-before-SL of the
Smart-Targets geometry" — methodologically clean, but it answers a DIFFERENT
question. Corrected on 2026-07-06 (`tools/mis1_move_labels.py`, `--label-mode move`).

**Status legend:** ✅ intent confirmed · ✏️ intent phrasing needs Michi's
confirmation/correction · ⚠️ drift between intended and actual · ⛔ empirically
refuted/off.

Sources: operator statements (chat 2026-07-05/06), `audit_reports/16_strategy_concept_evaluation.md`,
`audit_reports/dossiers/*`, Report 19 / `staging_models/REPORT.md`.

---

## 1. MIS1 — pump/dump early warning from indicator combinations ✅ (intent confirmed by the operator 2026-07-06)

**Intended (operator's words, paraphrased):** combinations of indicator readings
(e.g. RSI high >60, price far above EMA/WMA, volume falling) predict an upcoming
move: **±5% within 8h, ±10%/24h, ±15%/72h, ±25%/168h** — the larger the horizon,
the higher the hit chance. Threshold/confidence tuned so that few but safe trades
result (max PnL at min risk).

**Actual:**
- Features = 63 cleaned indicator readings (`core/mis_features.py`) → covers the
  intended scope.
- Batch-E retrain used the geometry label (TP1-before-SL) → answers "does the
  posted trade pay off?", NOT the intended question. ⚠ fixed: move-label mode
  (`tools/mis1_move_labels.py` + `retrain_from_replay.py --label-mode move`,
  thresholds `MOVE_THRESH_PCT`), threshold pick `pick_threshold_safe`
  (avg PnL/trade, min. 200 val trades, "not deployable" as an honest result).
- Both model sets remain comparable in staging: `mis1_move_model_*` (intended
  question) vs. `mis1_model_*` (trade geometry question).

**Decided (Michi, 2026-07-06):**
- [x] Move basis: **train BOTH variants** (close and wick, `--move-basis`),
      the test-result comparison decides. Artifacts `mis1_move_model_*` (close)
      vs. `mis1_move_wick_model_*`.
- [x] Product remains a **trade signal with Smart Targets**: the move model
      picks the candidates, economics are measured against the geometry.
- [x] Transition (REVISED on the afternoon of 2026-07-06): **MIS1 will be
      SHUT DOWN with the MIS2 go-live** — no parallel operation. The
      out-of-time tests of the move models (all 4 pump horizons positive)
      count as proof.
- [x] **MIS2 deployed 2026-07-06**: pump side only (LONG), basis mix close
      for 8h/24h/168h + wick for 72h, tags `MIS2-<H>H`, same horizon channels.
      Bot 11 with no legacy fallback (MIS1 models no longer load).
- [x] **Dump side reworked and LIVE (2026-07-06 evening):** geometry study
      in two rounds (`tools/mis2_dump_geometry_study.py`, results V1/V2 in
      `staging_models/mis2_dump_geometry_study*.json`):
      V1 (market entry, SL ≤8%) — all negative, diagnosis: selected coins twitch
      upward before the dump and blow out the stops. V2 built on operator input
      ("more SL headroom") + structural analogy to EPD1/RUB1: **limit entry 5%
      above the signal price (selling into the bounce) + horizon-dependent wide
      SLs** turns 24h/72h/168h positive.
      **Deployed rules (all: entry limit +5%, close-basis models,
      operating point top-2% val quantile):**
      8H TP−5/SL5 (study −0.24%/trade — operator wants live proof, objection
      documented) · 24H TP−10/SL16 (+0.49) · 72H TP−15/SL12 (+0.72) ·
      168H TP−16.7/SL12 (+0.27).
      **Operator decisions:** posts at 20x (cross, small positions on a large
      account — deliberately NO cap_leverage_to_sl despite SL > isolated
      liquidation distance); all 4 horizons as trades (no warning channel).
      **Known follow-up work:** the trade monitor doesn't know about limit
      entries — MIS2-SHORT scoring must detect "entry never filled" (price
      doesn't reach +5%, 12–22% of signals), otherwise phantom trades get
      scored.

---

## 2. ABR1 — break & retest ✅ (intent confirmed 2026-07-06)

**Intended (confirmed):** after a S/R level breaks, the FIRST retest of the
level holds → continuation in the break direction; if the breakout fails (price
falls back above/below the level), that's the losing class. ML filters
continuation from failed breakout.

**Actual:** the detector rework on 2026-07-05 aligned live detection with this
idea for the first time (direction-coupling of the retest, hold check, first-touch
only, no repainting edge pivots, only the most recent closed candle) + 5 setup
geometry features. Walkforward on the new detector is running. **No concept drift —
on the contrary, the old implementation deviated from the idea** (failed breakouts
were signalled as entries). Label = TP1-before-SL of the posted geometry: for a
trade filter, this is intent-compliant.

**Decided (Michi, 2026-07-06):**
- [x] Intent statement confirmed; label stays trade geometry (TP1-before-SL) —
      the right question for a detector filter.
- [x] ~~**LONG side always stays open**~~ **REVISED on the evening of 2026-07-06
      (Michi):** the always-on LONG bypass produced ~60 signals in 3h across the
      657-coin universe; Report 21 (exit resim + ML selection + BTC regime on
      27.7k events) shows: setup unfiltered −0.59%/trade, break-even WR ~63%, no
      tested leverage turns LONG positive. LONG runs again through the legacy
      blocker (3-class model with no meta.json, gate 0.60 ≈ closed).
      Reactivation only with new data sources or a regime change (Report 21 §3).
- [x] **LONG funding-gate EXPERIMENT (Michi, 2026-07-06 late evening):** after
      the feature recheck on the operator's hypothesis ("wrong indicators"), 16
      setup-mechanics features + 6 funding features were tested (Report 21
      Addendum 2). The only out-of-sample survivor: **fund_24h > +3 bps** (longs
      pay a premium above the Binance default) → +1.12%/trade, 74% WR
      (n=119/year on 100 coins; test +0.69%, n=17 — thin). LONG now opens ONLY
      through this gate (live REST, fail-closed, 30-min cache), posts as ABR2
      with the funding value in the info message. Expectation ~1–2 signals/day.
      **Review after 4–6 weeks** (≥30 trades): Cornix tracking decides.
- [x] **SHORT funding veto (Michi, 2026-07-06):** mirror test on 33.5k SHORT
      events — `fund_24h > +1,5 bps` is consistently loss-making for SHORTs in
      both train AND test (−1.2%/trade; exactly the zone where the LONG gate
      opens → independent cross-validation of the funding signal). SHORTs now
      need model gate ≥0.75 AND fund_24h ≤ +1.5 bps; fail-open (the veto is a
      safety net, not the primary gate). Review together with the LONG
      experiment.
- [ ] Re-determine the Batch-E threshold (SHORT 0.75 from a thin validation)
      with `pick_threshold_safe` once the running sim finishes.

---

## 3. TD — Three-Drive / RSI divergence ✅ (intent confirmed 2026-07-06)

**Intended (confirmed):** three consecutive higher highs (resp. lower lows)
whose RSI falls (resp. rises) at the pivots = momentum exhaustion → reversal
entry. Effectively an RSI-divergence strategy at multiple extrema. ML filters the
patterns; label = trade geometry (confirmed).

**Actual:** detector untouched (the bot's own detection is replayed); label =
TP1-before-SL of the posted geometry. The old trainer had a hindsight entry + fixed
2R geometry — the replay fix is a correction TOWARDS the traded reality. Batch-E
result: TD_4H a small real edge; TD_1H no learnable edge on the 20-feature set.

**Decided (Michi, 2026-07-06):**
- [x] Intent statement confirmed.
- [x] **TD_4H: deploy the staging model** (re-determine the threshold with
      `pick_threshold_safe` first; follow the rollout checklist in
      `staging_models/REPORT.md`).
- [x] **TD_1H: REDESIGN the ML gate** instead of parking it — don't keep the
      old gate. Starting points for the redesign: pattern-geometry features
      (divergence strength, drive symmetry, pivot spacings — analogous to the
      ABR1 setup features), 1h+4h pooling against the thin data situation,
      possibly a different target variable. Own task; until then TD_1H keeps
      running as-is.

---

## 4. BB — breaker block ✅ (intent confirmed 2026-07-06)

**Intended (confirmed):** broken support becomes resistance (and vice versa);
retest of the broken level → entry in the break direction. The best-supported SMC
idea; large enough for fees on 4h, not on 1h.

**Actual:** like TD — detector untouched, label now the traded geometry. The old
trainer had features on the wrong candle (breakout instead of retest) — fixed by
the replay. BB_4H: real ranking (+5 pp), but test PnL negative → filter use only.

**Decided (Michi, 2026-07-06):**
- [x] Intent statement confirmed.
- [x] **BB_4H: deploy the staging model as a filter** (re-determine the
      threshold with `pick_threshold_safe` first; the PnL lever remains the exit
      geometry).
- [x] **BB_1H: REWORK from scratch** (own task, analogous to the TD_1H gate
      redesign) — not just parking it. Working assumption until the rework:
      complete the parking (close the SHORT gap) so no half-parked state keeps
      firing — veto possible if SHORT is meant to deliberately stay open.

---

## 5. SRA1 — ML quality filter over Support/Resistance ✅ (intent + label semantics confirmed 2026-07-06)

**Intended (reconstructed):** not a signal generator of its own: the classic
S/R strategy produces the candidates, the ML only says "take this one / don't"
(meta-labeling). Label = the real trade outcome of the same strategy.

**Actual:** the conceptually healthiest setup in the fleet, no Batch-E retrain.

**Decided (Michi, 2026-07-06):**
- [x] Intent statement confirmed (pure meta filter).
- [x] **Label semantics CLARIFIED:** SL1/SL2/SL3 = SL hit after TP1/TP2/TP3 =
      trailing profit exits → label `WIN` is CORRECT. The open audit question
      (Report 13/16) is thus answered by the operator; no more label blocker.
- [x] ATR crash: was already fixed (P1.20). Label semantics additionally
      confirmed by code proof (13-updatesupportresistance counts targets
      reached).

**SRA2 retrain performed the night of 2026-07-06 — result: NOT deployable.**
`tools/retrain_sra2.py` (22 scale-free features, look-ahead fix incl. TZ
correction Europe/Bucharest→UTC, native NaN, isotonic + safe threshold;
7,967 events):
- LONG: test 448 trades @0.64 → WR 42.0% (baseline 38.5%, only +3.5 pp uplift),
  avg **−1.61%/trade** — val-test break; the test window (Jan–Feb 26) was a bear
  phase.
- SHORT: the safe picker honestly declines (no operating point with positive
  avg PnL at n≥100).
- **Root blocker discovered:** the label source `closed_trades3` has been DEAD
  since 23.02.2026 (writer 13-updatesupportresistance in _X no longer runs) —
  training data ends 4.5 months ago, S/R outcomes have gone untracked since.
  → Task #5: revive the label pipeline (replay labels preferred over the
  fragile tracker), THEN repeat SRA2. **SRA1 remains live, unchanged.**

---

## 6. ATS1 — TSI crossover sniper ✅ (intent confirmed 2026-07-06)

**Intended (confirmed):** a direction model is only queried on the TSI fast
crossover on the last closed candle (event gate). Architectural blueprint: live
scores exactly the trained event population.

**Actual:** no retrain so far; known defects: OBV train/serve skew (inverts the
confidence ordering), label 2.5%/1.5% bracket ≠ live geometry, data stale.

**Decided (Michi, 2026-07-06):**
- [x] Intent statement confirmed.
- [x] Operating band [0.60, 0.80): confirmed by Michi — was already
      implemented (12_ai_ats_bot.py:30-35, audit batch 03/04.07; ≥0.80 goes to
      shadow).
- [x] **Retrain scheduled** (queued after SRA1): scale-free OBV features,
      label = posted geometry via replay, fresh data, own walkforward adapter
      (event-gated like live).

**ATS2 retrain infrastructure built (T-2026-CU-9050-121):** DB-based via
`core.candles` (R1-clean, `include_forming=False`; no CSV — the old
`X8-TSI-EXPORT/-ML` scripts in `_X` are superseded). The shared feature builder
`core/ats_features.py` is called by both bot 12 AND the trainer
(`build_ats_features` → trainer==serving, proven by the parity test
`backtest/test_ats_features.py`) and thereby fixes the OBV train/serve skew;
label = first touch of the posted HVN/S-R geometry via `simulate_exit`
(`core.trade_utils.hvn_sr_trade_geometry`, byte-identical to the bot geometry)
instead of the old 2.5/1.5% bracket. Event-gated walkforward adapter
`tools/walkforward_sim.py --strategy ats`, training
`tools/retrain_from_replay.py --strategy ats` (or one-command
`tools/retrain_ats.py --days/--since`) → `staging_models/ats2_model_{LONG,SHORT}.pkl`
(`model_id=ATS2`, chronological split + 7d purge, `pick_threshold_safe`, isotonic
calibration). **Still NO VPS training run/deploy** — artifact generation +
rollout recommendation are Michi-gated (hard rule 2).

---

## 7. EPD1 — real-time pump ignition ✅ (intent confirmed 2026-07-06)

**Intended (confirmed):** 10s ticks: a sudden volume anomaly + micro-momentum =
ignition of a move → **ride along** (pump → LONG, dump → SHORT; no fading). One of
the few real short-term edges in alt perps.

**Actual:** the trainer only sampled `vol_ratio ≥ 5` events, live scored without
a gate (OOD) — the vol_ratio gate has since been implemented, but along with it a
"LONG only" direction gate (audit batch). The daily retrain is commented out but
still logs success. Profit strongly regime-dependent (alt-pump phases; July
negative → drift watch mandatory).

**Decided (Michi, 2026-07-06):**
- [x] Intent: riding momentum in both directions confirmed.
- [x] **Open the direction gate: BOTH sides run** (the "LONG only" is
      dropped; the vol_ratio ≥ 5 gate stays). → code change + bot restart
      needed.
- [x] Remove the false success logging of the commented-out daily retrain.
- [x] **Retrain scheduled** (label = posted geometry, only vol_ratio≥5
      events, drift monitoring because of regime dependency).
- [ ] **Add funding features to the retrain** (operator, 2026-07-06):
      `core/funding_features.py` (shared builder, Report 21 Addendum 2) — for
      ABR, fund_24h cleanly separates directional success (LONG gate >+3 bps,
      SHORT veto >+1.5 bps, cross-validated on 33.5k events); plausibly
      direction-decisive for a momentum-riding model. History is fully
      available in `funding_rates` (430d × 530 coins).
- [x] **Replay adapter BUILT (the night of 2026-07-06):**
      `tools/epd2_build_dataset.py` — EPD is 10s-tick-based, not replayable
      bar-by-bar; the detector logs (`pump_dump_events`) ARE the events.
      Mirrors bot-10 semantics: alert gate vol_ratio≥5 both sides, direction =
      ride along, 900s dedup, post-spike entry, **HVN/SR geometry as-of**
      (`get_hvn_and_sr_levels(df=…)` + `hvn_sr_trade_geometry`), label via
      `simulate_exit` (skip entry hour, 7d horizon), 10 live features
      (sample_fill=1.0 as a documented approximation) + 6 funding features.

**EPD2 retrain performed 2026-07-07 — BOTH directions NOT deployable.**
Dataset after the DST fix: 85,031 events / 639 symbols (2026-02-25→07-07, no
more history is available — start of the logs), 78,351 labeled;
`retrain_from_replay.py --strategy epd` (16 features = 10 live + 6 funding,
chrono split, 7d purge, safe threshold):
- LONG (45,760 events, baseline 52.2%): the safe picker declines; best val
  point −0.97%/trade. Test calibration monotone in WR (43.9→69%), but **every
  bucket negative in avg PnL** — the model ranks TP1 probability, yet the
  posted geometry still has negative EV.
- SHORT (32,591 events, baseline 60.0%): val formally deployable @0.674
  (+0.09%/trade, wafer-thin), but **val-test break**: test 1,204 trades, WR
  68.2% == baseline rate (zero selection), −0.90%/trade.
- **Month split: not a single positive month in EITHER direction** (LONG avg
  −0.05…−3.66; SHORT avg −0.00…−3.93). Unlike RUB-LONG (§8), there is no
  bull-regime rescue anchor visible in the available window here either —
  though the 4.5 months contain no strong alt-pump phase (EPD1s profitable
  phases, per the step-4 measurement, lay before this).
- Consequence: no deploy; the as-is state (bot 10 with the old model, both
  directions open per operator decision) keeps running, drift watch remains
  mandatory. Artifacts sit in staging (`epd2_model_{LONG,SHORT}.pkl`), stats
  `retrain_epd2_stats.json`. Follow-up: retry the retrain once a real alt-pump
  phase is in the logs (regime-window thesis §8).

**EPD2 DB path audited (T-2026-CU-9050-121):** confirmed already DB-based,
R1-clean and CSV-free — `tools/epd2_build_dataset.py` reads the events from
`pump_dump_events`, the entry from `ticker_10s`, and geometry/indicators via
`core.candles` (`read_candles_with_indicators(include_forming=False)`), writes
JSONL (no CSV) to staging. **No candle-based pump trainer is possible** — the
live features are 10s-tick-based (not reconstructable from 1h OHLCV, hard rule
7); the event-log route IS the DB retrain. No fix needed; new for symmetric
one-command operation: `tools/retrain_pump.py --days/--since` (chains build +
`retrain_from_replay --strategy epd`). Follow-up unchanged.

**EPD retrain on the post-P1.39 definitions NOT EXECUTABLE — calendar
(T-2026-KYT-9050-004, 2026-08-01).** Report:
`docs/T-2026-KYT-9050-004-epd-retrain-feasibility.md`, numbers:
`staging_models/replay/epd4_feasibility.{json,md}`. No artifact produced.
- **Cut point demonstrated:** the hourly `pump_dump_events` count on
  2026-07-10 breaks at the hour of the bot-10 restart (17:08:29Z) from 56–170/h
  to 10–33/h. The restart armed P1.39, the T-035 rate normalization and the
  revived hourly warmup together at once; the event rate dropped ~5× (a gate
  effect, not a feature effect).
- **Dataset clean but too short:** 4,698 of 4,712 events written (0.3% loss),
  4,327 labeled, span 22.0 d. `chrono_split` gives val/test each the 15% band =
  3.3 d, the 7-day purge gap (= label horizon) eats it entirely →
  `split 1664/0/0` (LONG) resp. `1364/0/0` (SHORT).
- **The hard upper bound is `ticker_10s`,** not the cut point: the builder has
  taken the entry from there since T-2026-CU-9050-035, and the hypertable starts
  at 2026-07-07 11:19Z. The Feb–July dataset (85 031 events) is **no longer
  reproducible** with today's builder. Retention 365 d ⇒ the window grows.
- **The shift is smaller than market drift:** the two-sample KS per feature
  (14 d before/after) falls, for ALL four inputs, within the null band of
  adjacent 14-day window pairs (KS 0.036–0.174 against a null-band median of
  0.058–0.080, null-band max 0.204–0.434). Only marginal distributions
  measured.
- **The deployed model holds up out-of-sample:** `epd3_model_LONG.pkl` on the
  post-cut events, AUC(TP1) 0.586 with monotone calibration (38.3 → 66.7% TP1),
  SHORT 0.537. No indication of a serving model broken by the definition change
  → bot 10 keeps running unchanged.
- **Tag reserved: EPD4** (EPD1/2/3 taken, checked against the variant
  registry, `shadow_gate` and the DB history). NOT YET registered in
  `core/shadow_gate` — without an artifact that would be dead configuration;
  the reservation itself is pinned in `backtest/test_retrain_model_id.py`.
  ⚠ the gate default is LIVE.
- **Follow-up 2026-11-09 → T-2026-KYT-9050-067** (~1000 val/test rows per
  direction, the first operating point with `min_n=200` backing). Command:
  `python tools/retrain_pump.py --since 2026-07-11 --model-id EPD4`.
- **Open before an EPD4 go-live:** the serving population is 2.7× (LONG) resp.
  5.4× (SHORT) denser than the training population — the builder's 900s dedup
  mirrors an alert throttle whose timer is only reset on the live-trade branch
  and is inert for a leg that doesn't post live.

---

## 8. RUB1 — Rubberband mean reversion ✅ (intent confirmed 2026-07-06)

**Intended (reconstructed):** extreme stretch from the "fair value" (≥8% from
the 90d regression + RSI/TSI extreme + Donchian touch) → trade the snap-back.

**Actual:** the ML layer is demonstrably noise (MACD 9/21 trained, 12/26 fed
live; random-split memorization). Live profit comes from the pre-filter + S/R
targets + SHORT tails.

**Decided (Michi, 2026-07-06):**
- [x] Intent statement confirmed (snap-back after a multi-extreme; ML separates
      snap-back from a continuing falling knife).
- [x] Retrain label: **geometry with SL path** (first-touch TP1-before-SL —
      the drawdown path is automatically included via the SL touch). Same
      infrastructure; needs a RUB1 adapter in the walkforward (replaying
      pre-filter events).
- [x] **Adapter BUILT (the night of 2026-07-06):**
      `walkforward_sim.py --strategy rub` — pre-filter/regression/9-feature
      contract lifted into `core/rub_features.py` (ONE source, bot 13
      refactored and uses it live; X-R1). Replay per closed 1h candle: 95d
      regression as-of, 4h cooldown per direction like live, geometry =
      `get_hvn_and_sr_levels(df=…)` + `hvn_sr_trade_geometry` +
      `ensure_min_tp_distance`, label via `simulate_exit`; the feature dict
      additionally contains the 6 funding features.
- [x] **RE-OPEN the LONG gate** (operator decision, revises the audit batch:
      the idea is symmetric, LONG weakness possibly an artifact of the broken
      ML). → code change + bot restart needed.
- [x] **Add funding features to the retrain** (operator, 2026-07-06):
      `core/funding_features.py` (shared builder, Report 21 Addendum 2).
      Especially interesting for mean reversion: extreme funding = an overcrowded
      side → snap-back candidate vs. a continuing falling knife. History fully
      available in `funding_rates`. → Implemented: 15-feature contract (9 rub +
      6 funding).

**RUB2 retrain performed on the morning of 2026-07-07 — LONG NOT deployable,
SHORT deployable @0.829.** Replay `rub_replay_365d.jsonl` (365d, 530 coins,
97,641 events; the run was interrupted by the VPS outage at 04:42 and finished
via `--resume` from coin 433), trainer
`retrain_from_replay.py --strategy rub --days 365` (chrono split + purge,
isotonic, safe threshold):
- LONG (52,081 events, baseline TP1 60.6%): val curve negative on ALL
  thresholds (avg −0.9…−1.2%/trade), the safe picker declines (threshold null,
  test 0 trades). This means the operator's hypothesis "LONG weakness =
  artifact of the broken ML" is NOT confirmed by the clean retrain — even the
  clean model finds no profitable LONG operating point. Calibration inverted
  in PnL: low prob buckets carry the best avg PnLs (tail snapbacks), i.e.
  TP1 probability ≠ expected value.
- SHORT (45,560 events, baseline TP1 73.9%): thr 0.829, val +0.25%/trade
  (WR 81.5%), test 680/4,725 trades, WR 81.9% vs. baseline 79.1%,
  sum +432 %P (**+0.64%/trade net**) — consistent with the known SHORT-tail
  finding. Top features: slope_trend, dist_to_trend, dist_ema200;
  fund_7d_cum/fund_72h in positions 5/6 (funding genuinely contributes).
- Artifacts: `staging_models/rub2_model_{LONG,SHORT}.pkl` + stats
  `retrain_rub2_stats.json`.

**Deploy (operator decision 2026-07-07): SHORT LIVE in bot 13.**
`rub2_model_SHORT.pkl` copied into the repo root (P1.35); bot 13 loads the
artifact contract (bot-25 pattern), builds the 6 funding features as-of from
`funding_rates` (lazy per event; missing history ⇒ 0 = `fillna(0)` parity)
and gates on raw predict_proba @0.829. Fallback legacy @0.85 if the artifact
is missing. Freshness infra: scheduled task "Kythera Funding Backfill"
(hourly; the table had no live writer). LONG keeps running unchanged on the
legacy model @0.75 (operator: gate stays open).

**Regime finding for the LONG side (monthly split of the replay,
2026-07-07):** the operator's thesis "LONG bites in a bull market" is
supported by the data — unfiltered LONG events: Aug 25 +3.9%/trade
(n=4,321), Sep 25 +2.4%, Apr 26 +3.0%, but Oct 25 −3.6%, Nov 25 −4.8%,
Jan 26 −3.4%. The swing is a regime effect, not a ranking problem of the
model (the event ranking stays worthless in the retrain too). Consequence:
LONG needs a **regime gate** (bull-phase switch) instead of an event gate —
a candidate for the HMM regime study T-2026-CU-9050-020 resp.
whitelist/ROM1 integration.

### 8a. MAX1 — high-conviction throttle over RUB2-SHORT 🔨 (T-2026-CU-9050-067, default-off)

**Intended (operator decision Michi, 2026-07-11):** RUB2-SHORT is the fleet's
strongest short edge (OOT +0.64%/trade net; live since 06.07.: 24 closes, 79%
TP1-WR, +4.2% avg — T-2026-CU-9050-044), but fires ~9×/day. For the **main
channel**, Michi wants **1-3 trades/day with a very high hit rate**. The throttle
is NOT implemented *inside* RUB2 (T-2026-CU-9050-050 → **wontfix**: RUB2 stays
unchanged in its channel). Instead **MAX1**: a dedicated bot that runs the same
model but only posts the strongest candidates.

**Mechanics (`34_ai_max1_bot.py` → `core/max1_gate.py`, pure selection):**
- **Clone, not a refactor:** detection/features/funding-as-of come from the
  shared builders (`core/rub_features.py`, `core/funding_features.py` —
  imported, not touched, X-R1), the geometry from the shared
  `hvn_sr_trade_geometry`. This means the measured RUB2-SHORT win rate holds
  exactly for the trades MAX1 places. Bot 13 stays byte-for-byte as it is.
- **Two-part throttle:** a high **minimum probability** (`MAX1_MIN_PROB`,
  default **0.93**, never below the artifact threshold 0.829) as the actual
  selector, plus a **hard rolling 24h cap** (`MAX1_MAX_PER_DAY`, default **3**)
  as a backstop. Rolling instead of calendar day — no midnight burst.
- **Selection per scan:** collect all candidates above the gate, dedupe per
  symbol (strongest wins), sort deterministically (prob desc, symbol), cut to
  the free slots of the 24h cap.
- **24h counter from `ml_predictions_master`** — shadow **and** live, so the
  cap bites in shadow exactly as it does live (a faithful preview). Contract of
  the MAX1 tag in this table: **one row per selection, never per rejected
  candidate** — the underlying predictions are already persisted by the RUB2
  scan under its own tag.
- **Scan at minute 15** (RUB2: minute 10) — the same closed 1h candle, just
  offset against the second full scan on the DB.
- **Posting** via `core.signal_post.post_ai_signal` (exactly ONE Cornix
  message, rule 4). Tag from the artifact's `meta.model_id` (rule 6 / trap 16).

**Artifact:** `max1_model_SHORT.pkl` (repo root, **promoted 2026-07-11** per
operator decision Michi; produced on the VPS with sklearn 1.7.1, load-verify
`True MAX1 0.829 15 True` — T-2026-CU-9050-070) — a copy of the RUB2-SHORT
model with `meta.model_id=MAX1`, produced by `tools/make_max1_artifact.py`
(model, feature contract, calibrator, val operating point verbatim; only the
identity changes). Re-generation after every RUB2-SHORT retrain **on the VPS**
(the source artifact's library versions live there); the promotion remains
Michi's decision per generation. Without an artifact, bot 34 runs in idle mode.

**RUB2 interaction (by design):** cooldown, dedupe and open-trade spaces are
separated by tag (`MAX1` vs. `RUB2`) — the two bots **do not block each other**.
Consequence: **double exposure on the same coin is possible** (RUB2 posts to its
channel, MAX1 additionally to the main channel; if Cornix trades both channels,
the position runs twice). That is the deliberate consequence of "RUB2 stays
unchanged" — Michi controls the position size of this overlap via the Cornix
configuration of the two channels.

**Gates (default-off, flip = Michi's decision):**
- `MAX1_LIVE_POSTING=0` (shadow-first; without the flip, only shadow rows).
- `CH_MAX1` unset ⇒ falls back to `CH_MAIN`; both unset (0) ⇒ shadow-only.

**Two reading notes for the shadow numbers** (they are the data basis of the
threshold decision — don't skip over them):
- The persisted `confidence` is the **raw** predict_proba — the same domain as
  the gate, the 044 curve and the RUB2 rows. The calibrated value only appears
  in the info message.
- The **shadow frequency is an upper bound**: live, a post writes an
  `ai_signals` row that blocks further selections of the same coin until close;
  in shadow this row doesn't exist, only the 4h cooldown throttles there. So
  shadow tends to show a little **more** posts/day than live — never fewer.
- MAX1 scans the **full coin universe** from `coins.json`, not the curated
  `MAIN_CHANNEL_COINS` list. The main channel therefore also sees alts it
  doesn't see today. A restriction would be a separate operator decision.

**Open / needs confirmation:**
- [x] **Shadow-gate numbers** (operator decision Michi 2026-07-11, goal =
      **maximum hit rate**, T-2026-CU-9050-070): `MAX1_MIN_PROB=0,85` +
      `MAX1_MAX_PER_DAY=3` — deliberately NOT the default 0.93. Live curve
      (06.–11.07., 44 posted/28 closed): highest WR in the band 0.829–0.85
      (81–82%, n=21–28); from 0.88 up, WR **drops** (60–71%) and only the avg
      PnL rises. ≥0.88 candidates also cluster in funding episodes (the 24h cap
      then delivers ~0.7/day). ~~Caution: the **replay curve is unusable for
      this gate** — live↔replay prob correlation −0.37 on matched signals,
      suspected funding feature skew (T-2026-CU-9050-071).~~ **Final** numbers
      after 1–2 weeks of shadow (then `ml_predictions_master` measures the
      cap-bound selection WR directly); if the WR inversion holds, also check
      selection order/prob band instead of a floor.
      **Correction 2026-08-01 (T-2026-KYT-9050-008,
      `docs/T-2026-KYT-9050-008-rub2-replay-skew.md`):** the suspected feature
      skew is refuted — the funding features are bit-exact across 229 matched
      signals, the −0.37 was the measurement window (06./07.07 sits on the
      RUB2 go-live, before that the tag carried a different model). From 12.07
      on, live and replay match exactly on 92–100% of the rows, **the replay
      curve carries weight again**. Also corrected along with this: "live sees
      0.93+" comes from the same pre-deploy rows — RUB2-SHORT has never
      reached 0.93 live (max 0.876 resp. 0.920 after the 14.07 retrain), the
      default 0.93 would have silenced MAX1.
- [ ] **Going live** (`MAX1_LIVE_POSTING=1` + Cornix configuration of the
      main channel) — after the shadow evaluation, exclusively Michi's
      decision. **As-is state checked 2026-08-01** (T-2026-KYT-9050-008,
      observation, not a decision): `.env` carries `MAX1_LIVE_POSTING=1`,
      `MAX1_MIN_PROB=0.829`, `MAX1_MAX_PER_DAY=100000`; `ml_predictions_master`
      has 308 MAX1-SHORT rows (11.07.–01.08.), all posted, max confidence
      0.9199. So the operator has gone live and in doing so chose a different
      regime than the 0.85 + cap-3 noted above — the throttle is de facto open.
      Reconciliation = Michi's decision.

---

## 9. AIM1 → AIM2 — meta-gate over all signals ⚠ (concept deliberately changed — needs confirmation)

**Intended AIM1 (historical):** stacking over all bot signals: market context
× swarm behaviour × source identity → success probability per candidate.
**Finding:** good idea, the architecture violated every prerequisite; the model
was reliably inverted (F). Not rescuable by a retrain.

**Actual AIM2 (rebuilt 2026-07-05, parallel session):** DELIBERATE concept
change — no longer a standalone alpha generator, but a ranker/gate over posted
source signals; label = first touch of the reconstructed geometry; runs
shadow-only (posting enabled via `AIM2_LIVE_POSTING=1`, the channel is not
traded).

**Decided (Michi, 2026-07-06):**
- [x] **Redefinition signed off as the new intended state**: AIM2 = ranker/gate
      over posted source signals. The AIM1 idea (standalone signal generator)
      is officially history.
- [x] **Rollout: IMMEDIATELY LIVE** — no further shadow phase; posting counts
      from now on (the flag was already active, Michi configures the trading of
      the channel in Cornix). Drift/calibration monitoring keeps running
      regardless.

### 9a. AIM2-TOPN — "top 1-3 of the day" as a high-conviction channel 🔨 (T-2026-CU-9050-051, default-off)

**Intended (from T-2026-CU-9050-031, path 2):** the structural path to "daily
1-3 trades, very high win rate". AIM2 already ranks the whole fleet (OOT gate
uplift −0.69 → +1.92%/trade @34% pass). Instead of "everything above the line"
(≈110/day), AIM2-TOPN selects **at most N (1-3) of the day's strongest
candidates** and routes them into a **dedicated channel/tag** (`AIM2-TOPN`, rule
6) — by construction, few, highly selected trades, separate from the base AIM2
posting.

**Mechanics (bot 15 → `core/aim2_topn.py`, shared pure logic):**
- "Top-N of the day" is only known ex-post → approximated via a high
  **minimum probability** (`AIM2_TOPN_MIN_PROB`, default 0.95, never below the
  base gate threshold) plus a **hard rolling 24h cap** N (`AIM2_TOPN_N`,
  default 1). Rolling instead of calendar day — no midnight burst
  (23:50 + 00:10 = 2·N in 20 min).
- Selection per cycle: only `trusted` (passed the parity guard) &
  `prob ≥ min_prob`, dedupe per (coin, direction, strongest), deterministic
  tie-break (prob desc, coin, direction), cap = `N − posts_last_24h`.
- 24h counter from `ml_predictions_master` (shadow **and** live), so the cap
  bites in shadow exactly as it does live → a faithful preview.
- Posting via the audited `core.signal_post.post_ai_signal` (exactly ONE
  Cornix message, rule 4). The TOPN tag is excluded from AIM2s own candidate
  and swarm stream (F6 self-feedback).

**Gates (all default-off, flip = Michi's decision):**
- `AIM2_TOPN_ENABLED=0` (master switch; off ⇒ **zero** behaviour change to base
  AIM2).
- `AIM2_TOPN_LIVE_POSTING=0` (shadow-first, analogous to `AIM2_LIVE_POSTING`).
- `CH_AIM2_TOPN` unset ⇒ forces shadow-only (no fallback to the AIM2 channel).

**Open / needs confirmation:**
- [ ] **Threshold calibration** from the VPS DB via `tools/aim2_topn_calibrate.py`
      (read-only): which `min_prob` historically delivers ~1-3/day? Until then
      the conservative default 0.95 runs.
- [ ] **Going live** (`AIM2_TOPN_ENABLED=1`, then `AIM2_TOPN_LIVE_POSTING=1` +
      set `CH_AIM2_TOPN` + Cornix configuration of the channel) — exclusively
      Michi's decision after a shadow evaluation.

---

## 10. UFI1 — dead-cat-bounce short ⚠ REACTIVATED on operator decision (2026-07-06)

**Intended:** short dumped coins on the retracement bounce (daily).

**Decided (Michi, 2026-07-06):** **re-activate in the as-is state (20x),
deliberately as a "crash-month lottery ticket" with small positions.** The
objection was raised and overruled — documented: an honest walk-forward shows
11/12 months negative (~14% WR), +185R came from October 2025 alone, and at 20x
the liquidation (~+5%) sits BEFORE the SL (25–40%) — 72% of the historical trades
would have been liquidated. Keeping the position size small is the operator's
call (Cornix configuration). No retrain; un-parking + restart as the action item.

## 11. ATB1 → ATB2 — convergence-channel breakout 🔨 REBUILT, design merged (2026-07-07)

**Intended (Michi 2026-07-06, extended 2026-07-07):** "the" trendline = **line
through confirmed swing pivots with ≥3 touches** (1h/4h, objectively
reproducible). Per operator decision 2026-07-07, merged with the event
definition from the TradingView script "Breakout Pattern Setup
[WillyAlgoTrader]" (open source): **converging channels** (wedge/triangle/
pennant) instead of single lines — boundary fit to confirmed pivots, validation
via convergence (≥2% narrowing), channel width 0.5–120× ATR, touch tolerance
0.15× ATR and **volume contraction in the channel** (in-channel volume < 85% of
the run-up — untested by us so far). Event: breakout with a confirmed candle
close.

**Deliberate deviations from the script:** min touches 3 instead of 2
(operator intent); the 5-factor score (penetration depth/ATR, body ratio, body
commitment, volume spike, RSI momentum) is NOT adopted as a hand-weighted
score, but as **5 setup features for the XGB gate** (analogous to the ABR
geometry features); targets = measured move (⅓/⅔/full channel width) as a
candidate AGAINST our smart targets in the replay comparison; the script's
break-even trailing is suspect (QM lesson: gives back profits) → simulate exit
variants instead of trusting it.

**Plan (task #7, after the current retrain queue):** build the channel
detector (no repaint: only confirmed pivots, closed-candle break), walkforward
adapter, labels = first touch with fees via simulate_exit, geometry comparison
measured move vs. smart targets, retrain per the standard scaffold (safe
threshold, model_id=ATB2). The old trainer (close regression lines) is
discarded; the bot stays parked until ATB2 is validated out-of-time. No
backtest trust in the script itself — its "win rate" is TP1-touch (the
Report-16 trap).

**Status (T-2026-CU-9050-104, 2026-07-12):** the labeling/training pipeline
is built and tested DB-free — `core/atb2_features.py` (channel detector + 5
setup features + channel geometry, shared bot/simulator/trainer),
`tools/walkforward_sim.py --strategy atb2` (measured-move label via
`simulate_exit`, smart targets for comparison) and
`tools/retrain_from_replay.py --strategy atb2` (per direction, 3d purge split,
isotonic, `pick_threshold_safe`, artifact `model_id=ATB2` → `staging_models/`).
Run book + verdict criteria: `docs/ATB2_REBUILD.md`. Open: the label/train run
on the VPS (behind T-061, sequential jobs); bot serving rewire + P1.45 +
un-parking only after a deployable out-of-time verdict (C-gate).

---

## 12. Support Resistance (classic) ✅ (intent confirmed 2026-07-06)

**Intended (confirmed):** repeated test of a S/R level + RSI divergence
between the first and current hit → reversal entry at the level, targets from
structural zones.

**Decided (Michi, 2026-07-06):**
- [x] Intent confirmed.
- [x] Approved fixes: **closed candle (R1) + TP interpolation fix (P0.7) +
      ATR-SL instead of a fixed 2.5% + drop the OBV component** (statistically
      no effect).
- [x] No direction gate: LONG stays open (SHORT carries the profit, but Michi
      wants both sides).

## 13. Main Channel (classic) ✅ (2026-07-06)

**Decided (Michi):** stays **separate** from Support Resistance — the double
exposure (same logic, two channels) is deliberate and intended. No merge. The
ATR-SL idea is still adopted into Support Resistance (see above).

## 14. Volume Indicator (classic) ✅ (2026-07-06)

**Intended (confirmed):** price at a 90d high-volume node + a fresh volume
spike gives direction → entry at the volume zone.

**Decided (Michi):** **rework approved** — save the real core: binned volume
nodes (instead of float-price summation), a freshness requirement for the spike
(hours instead of 5 days), per-coin cooldown, structural targets. Own task.

## 15. 5 Percent (classic) 🔨 REDESIGN commissioned (2026-07-06, retroactive)

**Decided (Michi):** **redesign with an earlier entry** — tackle the core
problem instead of symptoms: reduce the ~26 redundant conditions to a few
independent filters (trend establishment, but EARLY instead of exhausted),
move entry timing forward, add a time exit; fix the SHORT-headroom no-op
(P1.14) + the EMA typo (P2.43) along the way. Validation via walkforward
before switching live. Own task in the redesign queue (after QM/BB_1H/TD_1H).
Until then, the as-is state keeps running.

## 16. Fast In And Out (classic) ⚠ KEEPS RUNNING on operator decision (2026-07-06)

**Decided (Michi):** runs on **unchanged** — already deliberately reactivated
in April, reconfirmed today (the audit objection −25,843 net / "picking up
pennies in front of the steamroller" was raised and overruled). No taming
measures wanted.

## 17. Quasimodo QM_1H/QM_4H 🔨 REWORK BOTH (2026-07-06)

**Intended (confirmed):** liquidity sweep + structure break; retest of the
sweep zone (QML) as the reversal entry; ML takes the best X%.

**Decided (Michi):** **rework both TFs** (QM_4H too — don't stop it):
retrain per the standard scaffold (closed-candle pivots, CMP entry in the
label, respect the artifact's threshold) + an **exit redesign** (the current
geometry structurally gives back the 67% WR edge). Own task in the queue.

## 18. BR family BR1H/2H/4H 🔨 build an ML gate (2026-07-06)

**Intended:** break & retest without ML (pattern detector 7).

**Decided (Michi):** **re-open both directions** (the "SHORT only" gate from
the audit batch is dropped) and **build an ML gate over the BR signals** — the
BB_4H-vs-BR comparison (+565 with ML vs. −4,106 without) motivates exactly
that. Plan: replay BR events in the walkforward, geometry labels, a binary
model per TF/direction following the standard scaffold. → the gate revert is
an action item; the ML gate is its own task in the queue.

## 19. Mayank (FVG) ✅ (2026-07-06)

**Decided (Michi):** keeps running as a **pure info channel** — no tracking,
no profit expectation, no work on it.

## 20. BTC SMC 100x ⚠ (2026-07-06)

**Decided (Michi):** **100x stays, deliberately** (lottery-ticket character;
the audit objection that liquidation ~−0.9% sits before every SL was raised and
overruled). **Only instrument it** (ai_signals tracking) so the bot becomes
measurable for the first time. → instrumentation task.

## 21. SMC Forex/Metals ⚠ (2026-07-06)

**Decided (Michi):** runs **unchanged** (the audit's shutdown recommendation
overruled). No tracking, no repaint fix commissioned.

## 22. Regime detection ✅ IMPLEMENTED + LIVE (2026-07-07)

**Decided (Michi):** **split up the TRANSITION residual class** — the
mid-vola band (P40–P75) gets its own trend rule so TREND_UP/DOWN occur at all
and the 4D gating isn't disabled half the time.

**Implementation (2026-07-07, operator's pick following
`tools/regime_rules_study.py`):** vol-scaled mid-band rule **V2 K=1.5 with
hysteresis** in `core/regime_logic.py`: |ret_4h| ≥ 1.5×ATR_4h% → TREND_UP/DOWN;
an existing TREND holds until |ret_4h| < 1.0×ATR (hysteresis via
`prev_regime` = the effective regime from `regime_current`); TREND targets
need 3 instead of 2 debounce checks. Low-vola/HIGH_VOLA/CHOP rules unchanged.
- Study (430d, 7 variants): the current rule produced 3 TREND_UP episodes in
  430 days (100% <1h) — structurally dead, because ATR<P40 and |ret|>1.5%
  almost mutually exclude each other.
- Validation with the final rule (stateful, real classify function):
  TREND_UP 9.6% / TREND_DOWN 9.8% of the time (each ~415 episodes, median
  1.5h, flaps 21–25%), TRANSITION 41%→20.8%. **RUB-LONG in TREND_UP
  +1.65%/trade (n=1,378), 9/13 months positive** — negative only in the deep
  bear months Oct/Nov 25 + Jan 26 (bull flickers within a bear = a trap) →
  confirms the regime-gate thesis from §8, but is no bear immunity.
- Deploy safety: missing whitelist cells for the new TREND states default to
  open (`no_whitelist_entry`) — no mass-auto-close risk; the cells collect
  data from now on. Tests: backtest/test_regime_detector.py (27, incl. 7 new
  for mid-band/hysteresis/debounce-3).
- **Follow-up:** the §23 rework (shrinkage instead of default-open) belongs
  shortly after this; the RUB-LONG regime gate in bot 13 only after the
  whitelist data situation, or as an explicit TREND_UP switch (operator
  decision).

## 23. Bot regime analyzer / whitelist ✅ rework commissioned (2026-07-06)

**Decided (Michi):** switch the gate metric from WR to **net
expectancy/median**, plus **confidence interval/shrinkage + minimum n** (no more
default-open, no more flipping on noise). Own task.

## 24. ROM1 / orchestrator ✅ role clarification (2026-07-06)

**Decided (Michi):** ROM1 is recognized as a **standalone trading bot** (no
longer a "pure router"): its own trade history flows into the gate as evidence,
SL distances are capped (verify/tighten the 15% cap from the audit batch). Own
task.

**Open point (Michi, 2026-07-07 → task T-2026-CU-9050-020):** **HMM regime
study** — a Markov-switching model (3–4 Gaussian states on BTC-4h features
incl. funding) as a regime layer, in a direct A/B comparison with
`26_regime_detector` (§22) and ROM1 gating. Motivation: the common failure
mode of ALL Report-21 failures was regime non-stationarity; an HMM posterior
with state persistence is the principled version of what the heuristic
attempts. Test criterion: does ABR-LONG/RUB's monthly out-of-sample
performance depend on the states — and does the posterior beat the existing
classification as a gate feature? A context layer, not an alpha generator;
details in the task.

## 25. Intelligence layer (whale/funding) ✅ upgrade commissioned (2026-07-06)

**Decided (Michi):** whale flows + funding extremes get **fed into
regime/gate as features** (instead of dead data collection). Own task: feature
engineering + validation; the whale logger has been running again since the WS
fix.

## Working rules (from 2026-07-06)

1. Every future (re-)training references this file: the label must answer
   the model's intended question; deviation only with a documented operator
   decision.
2. Threshold selection everywhere follows the operator's criterion: few,
   safe trades (`pick_threshold_safe`; "not deployable" is a valid result).
3. Two questions, two metrics: hit rate of the intended question (e.g.
   move-WR) AND economics of the traded geometry (net expectancy) are ALWAYS
   both reported — WR alone is worthless as a KPI (Report 16, finding 1).
4. Classic rule-based strategies (Support Resistance, 5 Percent, …) and the
   meta layer (regime/ROM1) are concept-assessed in Report 16; they get
   entries here as soon as work starts on them.
5. **Always only ONE training/simulation job at a time** (operator rule
   2026-07-06): walkforwards, retrains, labelers strictly sequential — the
   machine carries the live fleet, parallel jobs drive CPU load up. New jobs
   queue up behind the running one.
6. **Versioned model tags** (operator rule 2026-07-06, applies to ALL
   reworked models): every retrain/rework generation posts under a new tag —
   MIS2-8H…, ABR2, TD2_4H, BB2_4H, SRA2, ATS2, EPD2, RUB2, QM2, … The tag sits
   as `model_id` in the artifact meta and is written by the bot into
   `ai_signals.model`. Trackers (sentiment-channel cross-tabs, dashboard,
   whitelist) match by prefix and show old vs. new separately — this makes the
   generational difference directly visible. Cooldowns deliberately stay
   cross-generation (no double-posting old+new on the same symbol).
7. **One Cornix-parsable message per signal**: info/chart messages must not
   repeat the Cornix block (double-post bug 2026-07-06 in bot 18 + 7, fixed —
   Cornix was opening two positions per signal).
</content>
