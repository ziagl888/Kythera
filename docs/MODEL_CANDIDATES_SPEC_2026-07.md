# Model Candidates Specification 2026-07 — Implementation Handoff

**Purpose:** specify every candidate from the 2026-07-12 research run
(`reports/model_ideas_research_2026-07.md`, task T-2026-CU-9050-102) such that
a follow-up agent (Opus) can take on the coding **without follow-up
questions**. Every candidate is to be cut as its own KB task (project 9050,
workflow per `docs/OPUS-HANDOFF.md` §2).

**Mandatory reading before the first edit:** `docs/OPUS-HANDOFF.md` (work
cycle, pitfalls, escalation) and the work rules in `docs/MODEL_INTENT.md` (the
label must answer the target question; `pick_threshold_safe`; report both
metrics; one-job rule; versioned tags).

## 0. Rules that apply to ALL candidates

1. **Batch-E discipline:** every idea is first made cheaply falsifiable
   (study/replay, ~1 day) before live code is written. "No edge" is a valid,
   documentable result (no-op-done). Template: T-2026-CU-9050-020.
2. **DB work only in a VPS session** (the build machine has no credentials),
   live tables strictly **read-only**, process priority BELOW_NORMAL,
   **one-job rule**: only ONE training/sim job at a time, queue new jobs
   behind the running one.
3. **Study scripts** go to `tools/` (ruff-excluded, lower lint bar, but not a
   free pass). Results as JSON/MD go to `staging_models/` or `reports/`.
4. **Replay/labels:** always use the `tools/walkforward_sim.py` infrastructure
   or its building blocks: `simulate_exit` (first-touch TP1-vs-SL, fees),
   `get_hvn_and_sr_levels(df=…)` + `hvn_sr_trade_geometry` +
   `ensure_min_tp_distance` for as-of geometry. Chrono split + purge gap,
   calibration + threshold via `pick_threshold_safe` on the validation set.
5. **Feature builders are shared** (X-R1): new feature families go into
   `core/<name>_features.py`, imported by study, trainer AND (later) bot.
   Missing columns ⇒ load error/idle, never `fillna(0)` as a contract
   substitute.
6. **Only closed candles** (R1); watch for DESC-sorted frames (newest row =
   index 0 on some paths). TZ: new tables TIMESTAMPTZ/UTC; when reading
   legacy columns check the TZ cluster (AUDIT_TODO P2.1–P2.6).
   `closed_ai_signals` contains ~357k duplicate rows — **deduplicate before
   every evaluation** (by signal identity; verify column names against the
   live schema (`docs/schema.sql`), e.g. (coin, model, open_time, direction) —
   document the approach in the script).
7. **Artifacts only go to `staging_models/`** with `meta.model_id` = the new
   tag. Promotion into the repo root, gate flips, unparking bots,
   fleet/bot restarts, `.env` changes: **exclusively Michi** (OPUS-HANDOFF
   §6).
8. **Reporting duty per study:** n, WR, avg. net PnL (fees!), monthly split
   (regime stability!), val-vs-test consistency. WR alone is worthless
   (Report 16, finding 1).
9. **Document a survivorship note:** our coin tables follow `coins.json`
   (active USDT perps); delisted coins are partly missing. Every cross-section
   study records this as a known bias source.
10. **Fee assumption uniform:** same as `walkforward_sim` (look up the taker
    fee + slippage model there and reference it, do not reinvent it).

**Data inventory (as of 2026-07-12):**

| Source | Scope | Note |
|---|---|---|
| `{SYM}_{tf}` candles | ~530 coins × 5m/15m/30m/1h/2h/4h/1d/1w | Retention: **5m only 1 month**, 15m–4h 1 year (6_housekeeping) — build intraday studies on **15m** |
| `{SYM}_{tf}_indicators` | ~120 indicators | RSI/EMA/MA/WMA/SMMA etc. |
| `funding_rates` | 430d × 530 coins | hourly backfill task; builder `core/funding_features.py` (6 features) exists |
| `pump_dump_events` | since 2026-02-25 | detector log (columns: `volume_ratio`, `price_change_60s`) |
| `ticker_10s` | since 2026-07-07 | hypertable, ~108 coins, 10s |
| `whale_data/*.json` | since 2026-07-05 | top 20, prints ≥ $25k, with taker direction (`m` flag) |
| `ai_signals` / `closed_ai_signals` | full fleet history | duplicate trap, see rule 6 |
| `ml_predictions_master` | shadow+live predictions | A/B basis (FIF1 pattern) |
| `regime_current` / `regime_history` | BTC regime, 5 classes | TREND classes only populated since the §22 rework on 2026-07-07 |
| **missing** | open interest, liquidations, order book, on-chain | OI: only 30d rolling via REST → K9 |

**Recommended order** (one-job rule; rationale in the specs):
K9 (time-critical, implementation without a sim job) → K3 + K8 (cheapest
studies, pure DB analyses) → K15 (exit-variant study on existing events) →
K1 → K2 → K5 → K4 (largest build) → K6 → K7 → K11; K13 (seeding data like K9,
without a sim job — can be built in parallel with the study queue) · K10/K12
wait for data maturity.

**Addendum 2026-07-12 (T-2026-CU-9050-105):** K13, K15 and the K6 TOTAL3
addition stem from the second research round (leaderboard research + two
operator YouTube videos, rule extracts in KB `ingest-c1e5112dea7f` /
`ingest-9f6511a5f951`); findings in `reports/model_ideas_research_2026-07.md`
§6.

---

## Tier 1 — immediately testable

### K1 · TSM1 — Time-series momentum on 6h aggregates (study → possibly bot)

**Type:** replay study, then operator decision on the bot. **Effort:** ~1–2
days study.
**Hypothesis:** a ROC lookback signal on 6h candles (riding momentum
long/short) has a positive net edge across the USDT-perp universe — even with
OUR geometry (smart targets + fixed SL) instead of the paper's ATR trailing.
**Evidence:** F8 (arXiv 2602.11708v1, claimed 2.41 Sharpe net; medium —
overfitting suspicion due to monthly re-optimization).

**Approach:**
1. `tools/tsmom_study.py` (new, read-only): load 1h candles per coin,
   resample to 6h (UTC anchors 00/06/12/18 — **not** local time; only full,
   closed 6h windows). Additionally run the same pass on native 4h candles
   (robustness check, not a resampling artifact).
2. Signal: `ROC_L = close/close[-L] − 1`. Event when `|ROC_L|` crosses a
   threshold (sign = direction). **Fixed grid, NO re-fitting over time:**
   L ∈ {8, 12, 16, 24, 32} bars × threshold ∈ {0, 0.5σ, 1.0σ} (σ = rolling
   standard deviation of ROC_L, 90d, as-of). Dedupe: max. 1 open event per
   coin/direction (re-entry only after exit), analogous to the 4h cooldown
   convention.
3. Dual labeling: (a) our geometry via `get_hvn_and_sr_levels(df=…)` +
   `simulate_exit` (this is the deployable truth); (b) paper approximation
   for comparison: time exit after H bars (H ∈ {8, 16, 28}) with a wide
   catastrophe SL of 15%. If (a) diverges strongly from (b), that is the
   quantified cost of the Cornix substitution (open question 3 of the
   report).
4. Evaluation per grid cell: n, WR, avg. net PnL, monthly split, val/test
   chrono split (threshold choice ONLY on val; touch test once).
5. Result JSON to `staging_models/tsmom_study.json` + short MD report.

**Stop criterion:** no cell with val- AND test-positive net PnL at n ≥ 200
test trades ⇒ paper falsified for our stack, document, park. **Pitfalls:**
resample TZ; survivorship (rule 9); do NOT emulate the paper's refitting —
that is exactly its overfitting vector.
**If positive:** its own follow-up task "Bot TSM1" (next free bot number —
no. 35 is reserved for K9; tag `TSM1`, 6h scan cadence,
`core/model_artifacts.py` loader if ML-gated, otherwise rule-based; standard
conventions: ONE Cornix message, cooldowns, monitor-8 tracking).

### K2 · XSM1/XSR1 — Cross-section momentum rotation & alt-pump reversal (study → possibly bot)

**Type:** two-stage study (portfolio level, then event replay). **Effort:**
~2 days.
**Hypothesis:** (a) XSM1: top decile of 1–2-week returns outperforms at a
1–2-week holding period (LONG). (b) XSR1: coins with a strong 4–12-week run
revert (SHORT). **Evidence:** F4 (structure high, exact spec 0-3 refuted —
hence a matrix instead of a single spec) + F5 (anchored variant, medium).

**Approach:**
1. `tools/xs_momentum_study.py` (new, read-only), 1d candles: formation
   window F ∈ {7, 14, 28, 56, 84}d × holding window H ∈ {7, 14, 28}d, weekly
   rebalance grid over the 430d.
2. Ranking per rebalance: F-day return. **Two signal variants:** raw return
   AND anchored variant (distance to the formation **low**, F5). **Two
   reference bases:** absolute AND market-neutral (coin return minus BTC
   return) — otherwise the study only measures beta.
3. Liquidity filter: exclude the lowest volume tercile (median 24h quote
   volume over F) — literature edges often live in untradeable micro-caps.
4. Stage 1 (portfolio): decile spreads close-to-close over H, net of fee
   assumption (rule 10) + short-side funding cost from `funding_rates`
   (shorts pay on negative funding!). Heatmap F×H per variant/direction.
5. Stage 2 (only for val-positive cells): event replay with our geometry
   (entry = first 1h close after rebalance, smart targets, `simulate_exit`)
   — only this is the deployable statement.
6. Result to `staging_models/xs_momentum_study.json` + MD report.

**Stop criterion:** no F×H cell in stage 1 with a val+test-consistent net
spread ⇒ structure does not replicate on 2024–26 perps, document.
**Pitfalls:** survivorship (strongest here!); holding exits in live
operation need the close-command path (FMR2 mechanic, see K4) or monitor
timeout — the design decision belongs in the bot follow-up task, NOT in the
study.
**If positive:** follow-up task per direction (tags `XSM1`/`XSR1`), weekly
posting cadence, candidate cap (e.g. top 5) as an operator parameter.

### K3 · FRL — Funding risk layer across our own fleet (study → orchestrator feature)

**Type:** pure data analysis, no model. **Effort:** ~1 day.
**Hypothesis:** fleet SHORTs opened at extremely positive funding have
systematically worse expectancy (squeeze mechanic, F2); symmetrically LONGs
at extremely negative funding. The ABR2 gate (fund_24h > +3 bps LONG / SHORT
veto > +1.5 bps, Report 21 addendum 2) generalizes fleet-wide.
**Evidence:** F2 (high, BIS) + internal precedent ABR2.

**Approach:**
1. `tools/funding_risk_study.py` (new, read-only): dedupe `closed_ai_signals`
   (rule 6), evaluate `core/funding_features.py` as-of entry per trade
   (fund_24h, fund_72h, fund_7d_cum + cross-section percentile of the entry
   time across all coins).
2. Buckets (e.g. quintiles + extreme zones >+3 bps / <−3 bps) × direction ×
   model tag: n, WR, avg. PnL, monthly split.
3. Report: for which bots/directions does funding separate success from
   failure out-of-sample across the months? (chrono-halving as pseudo
   val/test.)

**Stop criterion:** no bucket effect stable across both time halves ⇒ ABR
finding does not generalize, document. **If positive:** follow-up task
"funding dimension in the orchestrator gating (bot 28)" — caution: this is a
gating parameter change ⇒ **operator gate, Michi decides** (OPUS-HANDOFF §6).
**Pitfalls:** BIS convention "sell liquidations" = short side (inverted vs.
vendor dashboards); TZ of the entry timestamps.

### K4 · FMR2 — Funding-extreme MR with normalization exit (build from a finished design)

**Type:** builder + retrain + (on success) bot exit loop. **Effort:** ~2–3
days across several queue slots. **The design lives entirely in
`docs/NEW_IDEAS_BOTS.md` § "FMR2 — own exit path"** — that chapter is
binding; only the order and additions follow here:

1. Exit predicate + constants go to `core/research_features.py` (one source
   for builder AND bot): SHORT exit as soon as `funding_cs_pctl < 0.80` OR
   `funding_z_30d < 1.0`; LONG symmetric; time stop 9 settlements (3 days);
   the hard catastrophe SL stays.
2. `tools/fmr1_build_dataset.py` V2: label = PnL at the normalization/timeout
   exit (exit price of the settlement candle) — NOT first-touch TP/SL (that
   was the FMR1 bug).
3. Retrain via the `tools/new_models_train.py` scaffold (chrono split,
   purge, `pick_threshold_safe`), artifact `staging_models/fmr2_model_*.pkl`,
   `meta.model_id=FMR2`.
4. ONLY if val+test positive: bot-31 exit loop (close command via
   `send_telegram` → `telegram_outbox`, own rows via `DELETE … RETURNING` →
   `closed_ai_signals` `status='CLOSED_FUNDING_NORMALIZED'`, filter strictly
   on its own tag; an hourly scan suffices). Own channel via `CH_FMR1`
   (.env = Michi).

**Stop criterion:** val+test not positive ⇒ the S8 thesis is finally
falsified (in which case the bot rework is also moot — offline-first was the
point of the ordering). **Evidence context:** F3 — perp-only funding capture
is genuinely open after the post-ETF compression; that is exactly why the
clean test is valuable.

---

## Tier 2 — immediately testable, medium evidence

### K5 · LIS1 — Post-listing drift (study → risk filter and/or fade bot)

**Type:** cohort study + replay. **Effort:** ~1 day.
**Hypothesis:** freshly listed perps underperform in the first weeks to
months (F10). Minimal benefit: **LONG blacklist for young listings** (pure
risk filter); maximal benefit: fade-SHORT from day T after listing.

**Approach:**
1. Listing date per coin: pull `GET /fapi/v1/exchangeInfo` (`onboardDate`,
   UTC) once and cache it as JSON to `staging_models/`; fallback proxy: first
   candle of the 1h table.
2. `tools/listing_drift_study.py` (new): cohort = onboardDate within the
   data window; forward returns day 1→7/30/90/180 absolute AND minus BTC
   (fix the beta confound from the sources!); distribution, median, %
   positive.
3. Fade replay: entry variants day {3, 7, 14} after listing (limit
   +0%/+5%), smart targets SHORT, `simulate_exit`; **funding cost must be
   factored in** — fresh perps often have extreme funding, which the short
   side can end up paying.
4. Report including cohort size (at ~40–60 listings/year, n is small —
   report it honestly, do not fake significance).

**Stop criterion:** drift disappears after the beta adjustment or n is too
small for a statement ⇒ document only the descriptive finding. **Minimal
deliverable even without a short edge:** quantified recommendation "coin age
< X days ⇒ no LONG" as an orchestrator/bot filter (implementation = gating
change ⇒ Michi).

### K6 · BRD — Market breadth/dispersion as regime features (feature block + study)

**Type:** shared feature builder + validation study. **Effort:** ~1–2 days.
**Hypothesis:** breadth measures across the 530-coin universe (share of
coins > EMA200/EMA50, median 7d return, advance/decline, return dispersion
vs. BTC) beat or complement the BTC-only regime classification — and supply
the missing **regime gate for RUB-LONG** (MODEL_INTENT §22 validation:
TREND_UP +1.65%/trade, n=1.378, 9/13 months positive; gate thesis in §8).
**Evidence:** externally unresearched (Report §3 question 4); internally
strongly motivated (§8/§22/§23, HMM task T-2026-CU-9050-020).

**Approach:**
1. `core/breadth_features.py` (new, X-R1): as-of builder on 1d/1h candles +
   `_indicators` (EMA200 is available). Efficiency: ONE query per coin,
   aggregate in-memory — do not hammer 530 tables individually per
   timestamp; BELOW_NORMAL. **TOTAL3 proxy as a mandatory feature (addendum
   2026-07-12):** equal-weighted (and, as a variant, volume-weighted) price
   index across our own universe without BTC/ETH — level, distance to the
   90d regression, breakout flag. Practitioner gate "alt trades only when
   TOTAL3 is above the level" (source: KB `ingest-c1e5112dea7f`). Honesty
   note in the builder doc: we do not have real market-cap weights — the
   price index over ~530 perps is a proxy and must be documented as such.
2. `tools/breadth_study.py`: (a) features vs. forward returns of the
   RUB-LONG events from `rub_replay_365d.jsonl` (already available — no new
   sim run needed!); (b) features vs. `regime_history` classes (additional
   information yes/no, simple logit/tree diagnostics suffice); (c) monthly
   split.
3. On a positive finding, follow-up tasks: feeding the feature into the
   whitelist rework (§23), the HMM study (T-020) and/or an explicit
   TREND/breadth switch for RUB-LONG in bot 13 (**gate change ⇒ Michi**).

**Stop criterion:** no feature separates RUB-LONG months out-of-sample
better than the existing regime ⇒ document; the builder work still remains
useful as infrastructure (HMM task) — that then needs a deliberate decision,
not a silent one.

### K7 · MOM — Realized-moments feature block + skewness study (SKW1)

**Type:** feature builder + cross-section study. **Effort:** ~1–2 days.
**Hypothesis:** (a) realized skewness (rolling, intraday basis) negatively
predicts ⇒ short candidate filter (SKW1); (b) RV/kurtosis as an additional
feature block for upcoming retrains (ATS2, QM2, BR gate). **Evidence:** F7
(medium, two independent papers; mechanism story refuted — use only the
signs, no story).

**Approach:**
1. `core/moment_features.py` (new, X-R1): realized vol/skew/kurt from
   **15m** candles (5m has only 1 month retention — 15m = 1 year!), rolling
   windows {24h, 7d}, as-of, only closed candles; native NaN policy (XGB
   pattern P1.20).
2. `tools/skewness_study.py`: weekly decile sorts (reuse K2's methodology
   scaffold: market-neutral, liquidity filter, funding cost), direction:
   short-high-positive-skew vs. long-low-skew; plus RV/kurtosis sorts as a
   byproduct.
3. Feature-block integration: optional `--features moments` hookup in
   `tools/retrain_from_replay.py` analogous to the funding block (6 funding
   features as the model) — **only build the hookup, do not trigger a
   retrain** (queue).

**Stop criterion:** skew deciles without a stable net spread ⇒ SKW1 dead;
the feature block remains as a retrain option (usage is decided by the
respective retrain task). **Pitfall:** MAX-based shorts are contraindicated
by F6 — do not "accidentally" build MAX instead of skewness.

### K8 · SET — Settlement/time-of-day study across our own fleet

**Type:** pure data analysis. **Effort:** ~0.5 days (cheapest study in the
catalog).
**Hypothesis:** entry proximity to the funding settlements (00/08/16 UTC) or
time-of-day windows affects the expectancy of our trades (F9: spread/vol
patterns around settlements). **Evidence:** F9 (medium, only 2 months of
data, dispersion ≠ returns — hence we test on OUR trades).

**Approach:** `tools/settlement_timing_study.py` (new, read-only): dedupe
`closed_ai_signals`; per trade, entry offset to the nearest settlement
(−240…+240 min in 30-min buckets) + entry hour UTC; expectancy per bucket ×
direction × model tag; bootstrap CI (simple resampling, no significance
theatrics). **TZ pitfall:** entry timestamps are partly naive-local — read
the TZ cluster P2.1–P2.6, convert offsets DST-aware (f95f092 pattern).

**Output:** recommendation table "bot × window avoid/prefer" —
implementation (scan-minute shift or posting window) per bot as mini
follow-ups.
**Stop:** no stable buckets ⇒ document, done.

*(Numbering note: K14 is deliberately not assigned — the original K14
placeholder "Ichimoku rule family" was discarded after the video review
yielded no Ichimoku rules; not a missing section.)*

### K15 · SRX — Scratch-reload-exit study on ABR/BR events (addendum 2026-07-12)

**Type:** exit-variant replay on existing event populations. **Effort:** ~1
day.
**Hypothesis:** for break-&-retest setups, a "scratch-reload" scheme beats
the fixed SL: exit immediately when a candle closes BACK below the entry
level (small scratch loss + fees), re-entry on the next cross+retest of the
same level, max. N cycles — instead of taking a full 4–12% SL hit.
Practitioner math: 10 scratch cycles ≈ 1% fees vs. one 4–12% SL hit.
**Evidence:** practitioner rule without a backtest (source: KB
`ingest-9f6511a5f951`, YouTube d5KlwDnJAAc) — pure Batch-E falsification;
the entry itself is our ABR concept, ONLY the exit mechanic is new.

**Approach:**
1. `tools/scratch_exit_study.py` (new): reuse the event population from the
   existing ABR/BR replays (ABR walkforward events from Report 21 /
   `walkforward_sim --strategy abr1` outputs — the event records carry
   `level_price`, `entry`, `sl`, `targets`, `signal_time`; no new detector).
   Simulate three exit variants per event:
   (a) standard geometry (as-is, fixed SL, first-touch) — baseline;
   (b) scratch-reload: **trigger field = `level_price`** (the broken level —
   that is the "line" of the practitioner rule, not the fill price `entry`):
   LONG exit on a 4h candle close back below `level_price`, re-entry on a
   renewed cross + retest of the same `level_price` (retest = the following
   candle does not close below the level); SHORT exactly mirrored (close
   back above `level_price`). N ∈ {2, 4, 8} cycles, fees per cycle per
   rule 10, time window per event 14 days;
   (c) like (b), but SL trigger close-based instead of touch-based — **its
   own grid cell, reported separately**, because close-based stops
   underestimate liquidation risk under leverage (liquidation is
   touch-based; cross-margin softens but does not eliminate this).
2. Comparison metrics per variant: avg. net PnL, distribution tails (the
   scratch approach trades rare large losses for frequent small ones —
   report median AND p5/p95), cycle statistics, monthly split.
3. Report to `staging_models/scratch_exit_study.json` + MD.

**Stop criterion:** variant (b) does not consistently beat (a) in val+test
(chrono split of the events) ⇒ practitioner thesis falsified, document.
**Cornix fit (relevant only on a positive finding):** SL-on-entry is native
to Cornix; the scratch exit + re-entry needs bot-side close commands +
re-posting (ROM1/FMR2 mechanic) and a monitor concept for re-entry trades —
its own follow-up task, only after the replay proof. **Pitfall:** the trade
monitor today knows neither scratch exits nor re-entries — the study is
deliberately offline; do not build any of this into bot 8 "on the side."

---

## Tier 3 — seed data now, harvest later

### K9 · OIC — Open-interest collector ⚠ time-critical (infrastructure)

**Type:** collector process + hypertable. **Effort:** ~1 day. **Why first:**
Binance REST holds OI history for only ~30 days — every day without the
collector is irretrievably lost history (backfill impossible; the same
lesson as ticker_10s: accumulate honestly instead of source skew).

**Approach (blueprint = `core/ticker_10s.py`):**
1. `core/oi_5m.py` (new): hypertable `oi_5m` (`ts TIMESTAMPTZ NOT NULL,
   symbol TEXT NOT NULL, open_interest DOUBLE PRECISION, oi_value_usdt DOUBLE
   PRECISION, PRIMARY KEY (ts, symbol)`); Timescale jobs: 1-day
   chunks, compression after 3 days (segmentby=symbol), retention 730 days;
   kill switch `KYTHERA_OI_PERSIST=0` (default on); batched insert.
2. Writer: **its own lean process** `35_oi_collector.py` (no attachment to
   the detector — separate failure domain): every 5 min a sweep over the
   `coins.json` symbols via `GET /futures/data/openInterestHist` (period=5m,
   limit=1, small weight) OR `GET /fapi/v1/openInterest`; document the rate
   budget (530 requests/5 min ≪ 2400 weight/min, with backoff retry per
   `core` conventions). Registration in `core/fleet.py` (+2 PG connections
   per process — check P1.34 against `max_connections`).
3. **One-time initial backfill:** read in the available ~30d
   `openInterestHist` (period=5m, paginated) per symbol — the API does not
   give more than that.
4. Starting the process = fleet intervention ⇒ **Michi** (restart-marker
   mechanic `control/restart/` or does the watchdog pick up new
   `core/fleet.py` entries on the next cycle? — verify; when in doubt,
   operator restart).

**Model ideas built on this (own tasks from ~Oct 2026, ≥60d history):**
OI-price divergence (price↑ + OI↓ = weak move ⇒ fade), OI-spike fade,
OI×funding interaction (refined F2 mechanic: squeeze susceptibility = high
OI + extreme funding).

### K10 · WHI — Whale-print imbalance (study, waiting for data maturity)

**Type:** persistence review now, study from ~4–6 weeks of history.
**Hypothesis:** taker-direction imbalance of large prints (≥$25k, `m` flag
in the aggTrade stream from bot 19) over 5/15/60-min windows predicts
short-term forward returns on the top 20. **Evidence:** externally
unresearched (Report §3 question 4); data since 2026-07-05.

**Feasible now (small task):** review the `whale_data/*.json` format;
optionally persist to a `whale_trades` hypertable (query convenience, same
Timescale conventions as K9) — the logger keeps writing JSON, a migration
script reads it in. Expanding the universe beyond top 20 = more WS streams
⇒ **operator decision**.
**Study (later):** imbalance features as-of vs. forward returns 15m/1h/4h;
on a signal → feature for BTC regime/ROM1 or its own candidate.

### K11 · WSH1 — Wick-based stop-hunt reversals (study)

**Type:** event study + replay. **Effort:** ~1 day.
**Hypothesis:** candles with extreme wick geometry + volume climax
(liquidation-cascade proxy without a liquidation feed) mark short-term
reversal points: long lower wick → LONG bounce (mirror: upper wick →
SHORT). **Evidence:** externally only mechanism (TradingView "Liquidation
Cascade Detector" — ignore performance claims, F11/F12); internally: the
PEX1 lesson.

**Approach:**
1. `tools/wick_reversal_study.py` (new) on **15m** candles (5m retention!):
   parametrized event definition: `lower_wick ≥ k×ATR14` (k ∈ {1.5, 2, 3}) ×
   `volume ≥ m×vol_sma20` (m ∈ {3, 5}) × close recovery ≥ 50% of the wick.
   Entry = close of the event candle (closed!), direction with the bounce.
2. Two populations: all events vs. events ≤ 60 min after a
   `pump_dump_events` entry (cascade context) — separates "some wick" from
   "wick after a cascade".
3. Labels: `simulate_exit` with smart targets; report per standard (rule 8).

**Stop criterion:** no parameter cell val+test-positive ⇒ falsified. **Heed
the PEX1 lesson:** the information content sits in the intraday window
around the event — do NOT fall back to 1h context features; if 15m looks too
coarse, waiting for ticker_10s maturity (the PEX2 path) is the answer, not
1h.

### K13 · HLW — Hyperliquid whale-position collector + study (addendum 2026-07-12)

**Type:** collector (seed data) + later study. **Effort:** ~1–2 days
collector.
**Hypothesis (study, later):** (a) aggregated positioning of curated
Hyperliquid top wallets predicts forward returns on our Binance perp coins
(feature for regime/ROM gating); (b) naively following individual whale
position changes survives our stack's minute-scale lag. **Evidence**
(leaderboard deep research 2026-07-12, Report §6): Hyperliquid is the ONLY
venue with permanently public per-address transparency; skill persistence in
the top percentile is academically documented (Barber/Odean, Taiwan —
mimicking was OOS-profitable there), but NEVER replicated for crypto perps;
style labels from aggregate stats are unstable (36–40% over 4 weeks); the
viral whale-copy stories are unverified. Hence: seed data + modest feature
questions, NO copy-bot promise.

**Data access (verified 2026-07-12, may change):** unauthenticated public
`/info` API: `clearinghouseState` (position snapshot per address:
entryPx=average, signed size, leverage, liquidationPx, ROE, cumFunding;
weight 2 ⇒ ~600 polls/min/IP), `userFills`/`userFillsByTime` (history,
2.000/10.000-item caps), `userFunding` + `userNonFundingLedgerUpdates`
(500-item pages, ms cursor); WebSocket `userFills` push (cap ~10 unique
users per IP on user subscriptions — verify against the live docs before the
architecture is fixed, the cap only appeared in verifier evidence).

**Approach:**
1. Wallet curation (operator input): 10–30 addresses from
   Hyperdash/HypurrScan/CoinGlass leaderboards, document the selection
   criteria (PnL history, account age, no points-farming suspicion).
2. `core/hl_whales.py` + `36_hl_whale_collector.py` (Timescale conventions
   like K9): table `hl_whale_positions` (ts TIMESTAMPTZ, address, coin,
   szi, entry_px, leverage, liq_px, roe, position_value; PK (ts, address,
   coin)) + `hl_whale_fills` (address, coin, side, px, sz, fill_time
   TIMESTAMPTZ, tid BIGINT; **PK (address, tid)** — the Hyperliquid fill ID
   `tid` is the dedup key, because polled `userFills` windows overlap;
   insert via ON CONFLICT DO NOTHING). Curated wallet list as a
   repo-tracked `hl_wallets.json` (analogous to `coins.json`: address,
   label, intake date, curation note — no secrets, operator maintains it
   via PR). Poll cadence 60 s per address (stays well under the rate
   budget), kill switch `KYTHERA_HL_PERSIST=0`; registration
   `core/fleet.py`; process start = **Michi** (like K9).
3. **Known pitfalls (from the research, record in the collector doc):**
   agent wallets return empty states (track the master address);
   sub-accounts/vaults are NOT enumerable from one address; entryPx is an
   average without entry timing (timing comes from fills, not snapshots);
   address churn (whales change wallets) ⇒ the curation list is maintenance
   overhead.
4. **Study (after ~6–8 weeks of history, own task):** (a) aggregate
   features (net-long share, position-change flow per coin) vs. forward
   returns of our coins; (b) lag curve: PnL of a reconstructed position
   change at entry +1/+5/+15/+60 min — answers the copy question honestly.

**Stop criterion (study):** no aggregate feature with a stable forward
signal AND the lag curve flat/negative ⇒ idea falsified; the collector is
then deliberately shut down (operator) instead of collecting zombie data.
**Boundary:** Binance leaderboard (grey-market scraper, collapsing),
Bybit/OKX/Bitget (no read API), BitMEX (anonymized) are NOT a basis —
documented anti-paths, do not re-evaluate.

### K12 · TRM2 — Transition-resolution re-submission (unblocked)

**Type:** re-submission of a parked candidate. **Effort:** ~0.5 days once
mature.
TRM1 was blocked upstream (the detector never held TREND — the classes did
not exist, `docs/NEW_IDEAS_BOTS.md`). Since the §22 rework (2026-07-07,
mid-band rule V2 K=1.5 + hysteresis), TREND_UP/DOWN each occur ~10% of the
time. **Trigger condition:** `regime_history` count — ≥300 completed
TRANSITION→TREND transitions per target class since 2026-07-07
(realistically a few weeks). Then: `tools/trm1_build_dataset.py` again
(check whether the builder sees the new classes cleanly), `new_models_train.py --strategy trm1
--min-val-trades 20`. **Tag bump required:** `model_id` is in the
`STRATEGIES` map of `tools/new_models_train.py` hard-coded to `TRM1` —
set it to **TRM2** there for the new generation (versioning rule 6,
MODEL_INTENT work rules). Stop criteria as before (deploy gate in
NEW_IDEAS_BOTS.md).

---

## Not pursued (documented anti-candidates)

| Idea | Why not | Evidence |
|---|---|---|
| Delta-neutral funding arb (long spot / short perp) | needs a spot leg — not in the Cornix stack; post-ETF compressed, 2024/25 profitability disputed on both sides | F3, refuted list |
| Naive lottery-short on high-MAX coins | MAX effect inverted in crypto; if lottery-short, then skewness (K7) | F6/F7 |
| Equity-style 3–12-month momentum | flips into reversal in crypto after ~1 month | F4 |
| BB/KC squeeze as a standalone model | popular in the community, zero performance evidence; at most as a cheap side cell in K1's grid | F11 |
| Adopting TradingView "win rates" as evidence | >95% repaint; use only mechanisms as hypotheses | F12, Report 16 |
| PEX1 rehabilitation on 1h features / EPD2 retrain without an alt-pump window / RUB2-LONG as an event gate / SRA2 before the label-pipeline fix | already cleanly falsified internally — the research yields NO new rehabilitation evidence | MODEL_INTENT §5/§7/§8, NEW_IDEAS_BOTS |

## Task cut (proposal for the KB)

One task per candidate (title schema "K<N> <Tag>: <short goal>
(study|build)"), declare `touches`, order from §0. K4 (FMR2), K9 (OIC) and
K13 (HLW) are implementation tasks with escalation points (channel .env,
fleet process, external venue dependency); all others start as study tasks
whose follow-up tasks are only cut after a positive finding. No candidate
deploys anything without Michi's explicit decision. Bot number allocation:
35 = K9, 36 = K13 (reserved per addendum); further bots take the next free
number.
