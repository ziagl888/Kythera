# T-2026-KYT-9050-008 — RUB2 replay↔live feature skew: root cause

**Status:** 2026-08-01 · **Session:** VPS (SRV02), read-only on all live tables ·
**Tool:** `tools/rub2_replay_skew_probe.py` (report: `staging_models/rub2_replay_skew_probe.{json,md}`)

## Task and result in one paragraph

At issue was the finding from T-2026-CU-9050-070: for the same (symbol, candle) signals,
live confidence (`ml_predictions_master`, RUB2-SHORT) and replay prob
correlated at **−0.37** across 49 pairs — same model, same candle, so feature skew.
The hypothesis in the ticket was the funding features.

**The hypothesis is refuted.** The funding features are not skewed: all six can be
reconstructed **bit-exactly** for **all 229** matched signals from today's `funding_rates`
(mean|Δ| = 0.0 in each of the six columns). The −0.37 is an artifact of the
measurement window: the overlap period 06./07.07. sits on the **go-live day of
RUB2-SHORT**. On 06.07., the tag `RUB2` still carried a different model. From the
switchover point on, live and replay agree, from 12.07. on 92–100% of rows **exactly**.

## 1. How it was measured

Window 2026-07-01 → 2026-08-01, RUB2-SHORT. 229 matched (symbol, candle) pairs across 128
coins — 4.7× the 49 pairs the original finding rested on.

Two methodological points, because both can flip the sign of the result:

- **Join key instead of offset.** `ml_predictions_master.time` is `TIMESTAMP WITHOUT TIME
  ZONE`, beschrieben mit einem aware-UTC-`datetime` → Postgres cast it to the session
  timezone on write. The probe computes this exact cast back via
  `AT TIME ZONE current_setting('TimeZone')`, instead of hardcoding the −3h observed in
  the 070 report. The replay stamps `signal_time` = candle open + 1h, the bot scans at
  hh:10 and anchors on hh:00 — the key is thus the same hour on both sides.
- **Two artifacts, not one.** `rub2_model_SHORT.pkl` in the repo root is **not** the
  model that ran in the July window (see §3). The probe scores both candidates and
  reports per day which one reproduces the live confidence.

## 2. Root cause: the measurement window sat on the model switchover

| Day | n | Pearson | mean\|Δ\| | rows exact |
|---|---|---|---|---|
| 2026-07-06 | 26 | **−0,45** | 0,161 | 0 % |
| 2026-07-07 | 19 | +0,60 | 0,030 | 0 % |
| 2026-07-08 | 21 | +0,98 | 0,015 | 0 % |
| 2026-07-09 | 38 | +0,98 | 0,011 | 0 % |
| 2026-07-10 | 59 | +0,97 | 0,009 | 3 % |
| 2026-07-11 | 38 | +0,97 | 0,014 | 0 % |
| 2026-07-12 | 25 | **+1,00** | 0,0003 | **92 %** |
| 2026-07-13 | 3 | **+1,00** | 0,000 | **100 %** |

The original report's −0.37 is the mean over exactly the top two rows.
RUB2-SHORT was deployed with `07c8874` on 07.07.; the live rows before that come from
the old 9-feature legacy path, which posted under the same tag `RUB2`. The switchover
point is visible in the data: **2026-07-07 ~07:00 UTC** (= 10:00 local, commit 09:40 +
fleet restart). Before that, neither RUB2 artifact reproduces the live values,
after it the time-window-correct one does, at mean|Δ| 0.010.

A pooled correlation measure spanning a generation boundary measures the
generation switch, not the feature skew. That is the actual lesson of this finding.

## 3. Two premises of the ticket were already outdated by the time this session started

- **The replay has long since been regenerated.** `rub_replay_365d.jsonl` carries a
  generation time of **2026-07-14 10:47–11:52** — after the funding backfill (11.07.)
  and after the two look-ahead fixes in `walkforward_sim` (`ac49bc3` forming candle,
  `21a97a6` bfill-from-the-future, both 10.07.). Step 2 of the task ("regenerate the
  replay") was thus already done before this session began — **no sim run executed**
  (sequential-jobs rule kept).
- **The live artifact has changed along with it.** The same run produced a RUB2 retrain
  (`_X/staging_models/rub2_model_SHORT.pkl`, 14.07. 11:52, threshold **0.7929**,
  n_test 1844), which was promoted into the repo root and tracked into git with
  `14e1c6f` (20.07.). The July model (threshold **0.829**, n_test 4725) survives only as
  `staging_models/max1_model_SHORT.pkl` — the byte-identical MAX1 clone from 11.07. The
  probe therefore picks the matching artifact by fit, not by matching filename.

## 4. The remaining difference 07.–11.07. — and why it vanishes on 12.07.

After the switchover point, mean|Δ| stays at 0.009–0.015, dropping to 0.0003 on 12.07.
The jump is dateable: **`logs/rsi_rewrite_execute_20260712.log` shows an executed
Wilder RSI rewrite of the indicator history on 12.07. (11:26–21:02, "3831 tables, 88 426 142
cells written")** — step (2) of the P2.12 sequence. So for the window 07.–11.07.: the
bot read the old span-RSI when scoring, while the replay generated on 14.07. reads the
overwritten Wilder RSI for the same candles. From 12.07. on, both sides read the same
domain — and the match becomes exact.

That is exactly the mixed-history risk predicted in P2.12, measured here for the
first time: **≈1 percentage point of probability** on RUB2-SHORT. Independent
confirmation: today's stored `rsi_14` values match a Wilder recursion bit-exactly
across 8 coins × July, not the old `ewm(span)` formula.

The funding backfill from 11.07. sits in the same window and could carry the same
effect — the two can't be cleanly separated from today's vantage point, because
`funding_rates` carries no insert time. There is an upper bound though: setting the
**entire** funding block to its live fallback (0, what `funding_features_asof` returns
when history is missing) moves the probability by 0.039 on average — the remaining
difference sits below that for 76% of rows, above it for 55 of 229. Funding alone
therefore does **not** fully explain it.

## 5. Feature by feature: replay file against today's DB

All 15 model inputs, rebuilt from today's DB with the shared builders:

| Group | Result |
|---|---|
| `fund_last`, `fund_24h`, `fund_72h`, `fund_7d_cum`, `fund_pctl_90d`, `fund_trend` | **100% identical, mean\|Δ\| = 0** |
| `dist_to_trend`, `slope_trend` | **100% identical** on the replay window |
| `rsi`, `TSI_Line`, `TSI_Signal`, `MACD_*`, `atr_pct`, `dist_ema200` | identical up to float32 storage rounding (mean\|Δ\| ≤ 1.6e-6) |

Also checked, because it's **not** the same window: the replay regresses over the last
`95·24` **rows**, the bot over all closed rows in a 95-**day** window. On a coin with
candle gaps, these are different windows. Measured effect:
`dist_to_trend` mean|Δ| 3.3e-4, `slope_trend` 6.5e-6 — **no** measurable effect on the
probability (all substitution variants score identically). The difference is real
but irrelevant; noted here so the next session doesn't have to search for it again.

## 6. Step 3 — retrain economics and MAX1 calibration

The 070 report concluded: "Across 59 days of replay-OOS NO event reaches 0.93 (p99 =
0.879), while live 0.93+ occurs at ~1.1 posts/day" — from which the recommendation
`MAX1_MIN_PROB = 0,93` followed.

**The second half of that sentence comes from the same contaminated rows.** The live
confidence distribution per model generation:

| Generation | n | avg | p99 | max | ≥ 0.93 |
|---|---|---|---|---|---|
| before 07.07. 07:00 (legacy under tag RUB2) | 128 | 0,622 | 0,968 | 0,983 | 5 |
| RUB2 @0,829 (07.–14.07.) | 790 | 0,754 | 0,865 | **0,876** | **0** |
| RUB2 retrain @0,7929 (from 14.07.) | 753 | 0,748 | 0,892 | **0,920** | **0** |

**RUB2-SHORT has never live reached 0.93** — the five rows above it are without
exception legacy rows from before the deploy. So replay and live also agree in
distribution: the clean replay's test slice (n = 1844, exactly the split of the
retrain meta) sits at p99 0.841 / max 0.874 against live p99 0.865 / max 0.876. **The
replay curve is usable again for calibration** — the reason T-070 discarded it
doesn't exist.

Threshold curve on the test slice (62.3d, scored with the July artifact):

| Threshold | n | /day | WR % | avg PnL % |
|---|---|---|---|---|
| 0,829 | 44 | 0,71 | 93,2 | +2,76 |
| 0,85 | 11 | 0,18 | 100,0 | +3,53 |
| ≥ 0,88 | 0 | — | — | — |

### Current state of MAX1 (checked in person on 2026-08-01, not taken from docs)

From `C:\Users\Michael\Documents\Kythera\.env`: `MAX1_LIVE_POSTING=1`,
`MAX1_MIN_PROB=0.829`, `MAX1_MAX_PER_DAY=100000`. In `ml_predictions_master`: 308
MAX1-SHORT rows from 11.07. to 01.08., all 308 `posted=true`, max confidence 0.9199.

Two consequences, both **operator decisions** (§6 OPUS-HANDOFF), stated here only:

1. The documented default `MAX1_MIN_PROB = 0,93` would have produced **zero** posts
   in 21 days. The recommendation was built on the contaminated curve; the operator
   overrode it regardless.
2. With floor 0.829 and cap 100000, the throttle is de facto switched off — MAX1
   posts practically every RUB2-SHORT candidate. That's a different regime than the
   "0.85 + cap 3" noted in `docs/MODEL_INTENT.md` §8. Whether that's intentional is
   for Michi to decide; the replay curve above is the basis, and it holds again now.

## 7. Latent defect found (fixed)

`tools/walkforward_sim.py` built the epoch axis of the RUB regression with
`open_time.astype("int64") / 1e9`. That is not a unit conversion but a bet on the
**resolution** of the column: `astype` returns the dtype's counting unit. Under the
fleet environment (pandas 2.3.2 → `datetime64[ns]`) it's correct; under pandas ≥ 3.0
(`datetime64[us]`) the axis shrinks by a factor of 1000 → `slope_trend`, one of the
15 model inputs, comes out **1000× too large**, while `dist_to_trend` next to it
keeps fitting (the fit at the window end stays stable). Exactly this happened in this
session's first reconstruction run — factor exactly 1000.0 on all 229 events —, and
exactly this would have hit the next replay generation on a newer interpreter: a
train/serve skew that no feature contract sees, because the column is there and finite.

Fixed with `core.time.epoch_seconds()` (normalises to ns before dividing), applied in
`walkforward_sim` and the three study tools with the same pattern. **Byte-identical**
to the prior state under the fleet interpreter — verified, no silent behaviour
change. Pinned in `backtest/test_epoch_seconds.py` (mutation-checked: the old formula
fails 2 of 4 tests).

## 8. Deliberately NOT done

No replay run (already generated on 14.07. — a new run would have clarified nothing),
no retrain, no promotion, no gate flip, no restart, no write query against live
tables. `core/funding_features.py` stays **untouched** — the root cause doesn't sit
there, and a change would have been a live behaviour change without cause.

## 9. Open

- The separation between the RSI rewrite and the funding backfill in the 07.–11.07.
  window can't be resolved (`funding_rates` has no insert time). Order of magnitude
  clarified (≈1 pp), attribution not.
- Whether the 14.07. retrain should be run again after the 12.07. RSI rewrite is the
  open P2.12 question step (3) — the replay from 14.07. already sits **after** the
  rewrite, so the retrain on it is on a uniform RSI domain. The older part of the
  replay history is likewise single-domain due to the rewrite. No action apparent,
  but not formally checked.
