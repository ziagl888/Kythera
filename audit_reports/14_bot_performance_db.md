# 14 — Bot/strategy results from the live DB (Step 4)

**As of:** 2026-07-03 · **Source:** `closed_ai_signals` (AI bots) + `closed_trades_master` (classic) on the live VPS. `closed_trades`/2–5 are the frozen v1 generation (up to 24.02.) and were not re-evaluated.

**Methodology & caveats (important for reading the numbers):**
- PnL = price movement entry→close in %, direction-adjusted, **without leverage** (margin PnL would be lev-fold) and a flat −0.10% round-trip fee for "net".
- "Win" = at least TP1 touched (targets_hit ≥ 1 resp. classic status 1–4/SL1–3). **Note: win ≠ profitable** — that is exactly what the data shows (below).
- All figures are **monitor-generated** and inherit the known monitor bugs: P1.2 (trailing SL never trails → multi-target PnL distorted), P2.7 (only the most recent 5m candle checked → missed hits during downtime), P2.31 (monitor scores up to 21 targets, TP1–5 get published), P1.9 (regime close censors foreign trades). They are the most honest measure available, but no substitute for an exchange reconciliation.

---

## A. Integrity findings (first, because they affect every statistic)

1. **🔴 82% of `closed_ai_signals` is migration junk.** 357,483 of 434,396 rows are duplicates: 7,210 groups with identical (symbol, model, direction, open_time), extreme case BULLAUSDT/EPD1/SHORT with **2,327 close rows for one signal**. 364,641 rows carry the sentinel timestamp `open_time = 2026-02-24 12:43:59.65` (v2 go-live moment), and the "LEGACY …" re-closes date entirely from 01.–02.03. — a one-time migration/re-scoring event that closed the same old trades hundreds of times over. After dedup, only **12,646 genuine rows** remain of 352,315 LEGACY rows (avg −0.88%). **Fix:** unique index on `(symbol, model, direction, open_time)` + a one-time purge; until then, deduplicate every evaluation. The active era is nearly untouched by this (only AIM1: 3,125→3,047).
2. **Classic:** 11,383 duplicate groups (~11k excess rows, small relative to 363k). 162,941 rows without `close_price` are **exclusively old era ≤ 28.02.** — since v2, everything is written completely.
3. Status-vs-PnL consistency, classic: 2,918 rows (1.6%) with win status but PnL < −0.5% (trailing give-back/P1.2 effect); the reverse: 0.
4. `closed_ai_signals` contains dead name variants (`MIS1-72h_dump`, `MSI1-*`, `ATS1_Robust`), 100% censored — legacy vocabulary, should be removed along with the purge.

---

## B. AI bots — active era (24.02.–03.07.), deduplicated, n=59,823

Overall: **WR 61.1%, avg +0.77%/trade gross (+0.67% net), sum +45,827 price-% (net +39,844)**.

| Model | n | WR | avg PnL | Median | Σ net | Verdict |
|---|---|---|---|---|---|---|
| MIS1-72H | 11,822 | 63.9% | +1.44% | 0.00 | **+15,868** | workhorse; positive in every month |
| EPD1 | 4,392 | 72.8% | +3.34% | +3.63 | **+14,222** | strongest avg; almost all from May/Jun (+14.6k), Jul negative (−345) |
| MIS1-168H | 7,167 | 58.5% | +1.07% | −0.03 | +6,928 | positive, but weakening since May (WR 48/49/35) |
| RUB1 | 2,496 | 57.6% | +1.57% | −0.06 | +3,675 | sum comes from tail gains (p95 +33%) |
| ROM1 | 2,677 | 69.2% | +0.92% | +1.00 | +2,184 | orchestrator delivers genuine added value (+8pp WR, positive avg) |
| TD_1H / TD_4H | 2,794 | 57.3% | ~+1.0% | ≈0 | +2,387 | ok; TD_1H is also the best-calibrated model (Step 2) |
| ATS1 | 1,768 | 65.8% | +1.02% | 0.00 | +1,622 | positive despite trainer shortcomings (Report 13) |
| MIS1-8H/24H | 1,003 | ~52% | +1.4% | negative | +1,261 | small n, tail-driven |
| ABR1 | 110 | 63.6% | +3.15% | 0.00 | +335 | small; model genuinely has only 7 features (Report 13) |
| SRA1 | 396 | 69.9% | +0.44% | +1.12 | +134 | healthy, small |
| BB_4H | 2,162 | 61.2% | +0.36% | −0.05 | +565 | narrowly positive |
| QM_1H | 3,139 | 67.5% | +0.06% | −0.03 | **−139** | 67% WR and still ≈ 0 — TP1 wins give it all back |
| ATB1 | 306 | 65.7% | −0.46% | 0.00 | −172 | negative (matches the Report-13 verdict) |
| BR4H / BR2H / BR1H | 11,756 | 58–60% | −0.1…−0.3% | ≈0 | **−4,106** | the whole BR family is net negative; BR1H LONG 65.5% vs SHORT 49.5% WR |
| BB_1H | 3,909 | 55.7% | −0.18% | −0.17 | −1,089 | negative |
| QM_4H | 556 | 54.9% | −0.40% | −0.29 | −277 | negative |
| UFI1 | 35 | 25.7% | **−7.90%** | −3.22 | −280 | catastrophic (confirms P0.11) |
| AIM1 | 3,047 | 50.8% | **−1.02%** | −1.01 | **−3,399** | consistently negative — matches the inverted model (Report 13); Feb start at 24% WR |

**Patterns:**
- **WR is misleading.** Median PnL is ≈ 0 or negative for almost all models — TP1 touch counts as a win, but the trade often ends near break-even via trailing/SL. The sums come from the tails (p95). A model with 67% WR (QM_1H) is net negative, one with 58% (MIS1-168H) is clearly positive.
- **Direction asymmetries are large:** EPD1 SHORT 76.5% vs LONG 50.2% WR; BR1H LONG 65.5% vs SHORT 49.5%; RUB1 SHORT 63.9% vs LONG 48.7%. A direction gate per model would be a cheap immediate lever.
- **Regime drift visible:** the BR/BB family was strongly negative Mar–Apr and positive from May (but with mini-n, since regime gating now filters them out almost entirely); MIS1-168H below 50% WR since May. Monthly slices are in the analysis script's appendix.
- **Leverage not factored in:** bots with high leverage (R4 findings) turn "−0.3% avg" into real account losses; UFI1's −7.9% at 20x would be liquidation.

## C. Classic strategies — deduplicated, rows with a close price only (n=184,331)

Overall: **WR 62.7%, avg −0.07%/trade, sum −13,360 price-%** — the classic family is, in sum, a zero-sum-to-loss business overall, even though all "win rates" look > 60%.

| Strategy | n | WR | avg PnL | Median | Σ net | Note |
|---|---|---|---|---|---|---|
| Support Resistance | 1,917 | 63.5% | +0.41% | 0.00 | **+596** | the only net-positive one; SHORT (+0.66% avg) carries it all |
| Main Channel | 202 | 67.3% | −0.28% | 0.00 | −77 | small, ≈ 0 |
| Volume Indicator | 51,440 | 64.1% | +0.09% | −0.10 | **−705** | gross +4,439, fees eat it up; Feb/May/Jun positive, Mar/Apr −7.2k |
| 5 Percent | 19,385 | 71.1% | −0.20% | −0.05 | **−5,766** | 71% WR and clearly negative — the textbook win≠profit example |
| Fast In And Out | 111,387 | 60.6% | −0.13% | **+1.25** | **−25,843** | median positive, avg negative → rare, large loss tails (p5 −2.7 is misleading; the abs>50% outliers concentrate here) |

**Interpretation:** the classic strategies produce enormous signal volumes (FIFO 111k trades!) with tiny per-trade gains eaten up by loss tails and fees. For FIFO, the median is +1.25% (TP1 scalps work), but the losers are rare AND large — a classic case of "picking up pennies." Volume Indicator would be ≈ break-even with better exit/fee management. The censoring share (FORCE_CLOSED/DELISTED/REGIME) is 1–6% and additionally skews optimistically per P1.9.

## D. Consequences / recommendations

**Data hygiene (before any further evaluation):**
1. Unique index `(symbol, model, direction, open_time)` on `closed_ai_signals` + purge of the 357k duplicate/LEGACY rows (backup first). Same for classic (11k).
2. Archive legacy name variants (`MSI1-*`, `MIS1-*h_*`, `ATS1_Robust`).

**Portfolio decisions (based on realized figures + Report 13):**
3. **Stop/park:** AIM1 (inverted + −3.4k net), UFI1 (25.7% WR, −7.9%/trade), QM_4H, ATB1. Review: BB_1H, BR1H/BR2H (net negative; possibly keep only the LONG side for BR1H).
4. **Keep/focus:** MIS1-72H, EPD1 (but watch the July dip + Report-13 gate fix), MIS1-168H (watch drift), ROM1/orchestrator (genuine added value — should climb further after the P0.4 whitelist fix), TD_1H, ATS1, SRA1, Support Resistance.
5. **Direction gates:** close EPD1 LONG, close RUB1 LONG, close BR1H SHORT, check 5 Percent LONG (n=1,087 too small for 76% WR confidence).
6. Classic family: rework exits (trailing give-back + fees), otherwise only Support Resistance carries itself.
7. Change the KPI definition: use **avg net PnL/trade and median** as the dashboard's headline metric instead of "WR (TP1 touch)" — the current WR display rewards exactly the wrong behaviour.

**Next verification step:** exchange/Cornix reconciliation of a sample (e.g. 50 trades across models) against the monitor PnL, to quantify the P1.2/P2.7 distortion.
