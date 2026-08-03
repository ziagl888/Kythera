# Step 2 — Live DB Verification (VPS)

**As of:** 2026-07-03 · **Environment:** Live VPS, PostgreSQL 17 (+TimescaleDB) `cryptodata@localhost` · Fleet was **stopped** at the time of the check (clean watchdog shutdown 11:23 local).

**Upfront — code-state comparison (diff Kythera ↔ live):**
AST comparison of all 75 shared `.py` files: the live version `PycharmProjects\crypto_trading_bot_v2` is **identical to the Kythera import commit `b6735d9`** ("live state 2026-07-01"), the only difference: live has the Telegram channel IDs hardcoded, Kythera reads them from env vars (redaction). **All Kythera commits since the import are NOT deployed** (ruff fixes, `_apply_keepalive` fix, mplfinance RAM-leak fix, watchdog lifecycle fix, dashboard tri-state, `core/process_control.py`, regression guard). Only on live: `99_smc_paper_bot.py` (not audited).

---

## A. Foundation

| # | Check | Result |
|---|---|---|
| 1 | `SHOW timezone` | **`Europe/Bucharest` (UTC+3)** → all TZ findings are live-relevant |
| 2 | Schemas | `trade_cooldowns.last_posted_at` = **timestamptz** (P2.2: the WITH-TZ variant won the bootstrap); `active_trades_master.time/posted` naive + prices `REAL` (P3.12); `telegram_outbox` **has** `image_path` (the wide DDL won); `ai_signals.current_target_hit` = **INTEGER → P1.5 defused** |
| 3 | `max_connections` | **200** (not 100) — P1.34 mitigated, at 27 processes × maxconn 8 = 216 potentially still tight |
| 4 | Forming Candle | **R1 PROVEN**, see below |

**R1/P1.11 — forming-candle proof (empirical):**
Last stored `BTCUSDT_1h` candle (02:00 UTC): `V=1618.9, low=61485.3, close=61537.9`. Binance real: `V=3999.4, low=61271, close=61411.8` → a **~40% partial candle sits "finished" in the DB** and is never corrected (P1.11). The daily candle from 3.7. is in the DB with `V=9668` (real >37,976, previous days ~236k–264k). `BTCUSDT_1h_indicators` has a row **exactly on this partial candle** → indicators on forming candles confirmed.

**TZ mix directly proven (R3):** fleet shutdown 11:23 local = 08:23 UTC. `ml_predictions_master.created_at` (naive): max **11:23** → local time. `regime_history.ts` (naive): max **08:20** → UTC. **Two naive columns, two different semantics.** `closed_ai_signals.close_time` max 06:00 → mixed writers (P2.4 confirmed).

---

## B. New operational findings (not in the Step-1 catalogue)

1. **🔴 Data-ingestion wedge, 6 hours unnoticed (P2.47 live-proven).** Ingestion ran since 2.7. 16:46 without a restart, but **all** symbols end 05:00–05:25 local (02:00–02:25 UTC). The WS stream was dead for ~6h, the watchdog considered the process healthy, and the rest of the fleet kept **posting signals on 6h-stale indicators until 11:23** (outbox entries up to 11:23). Exactly the "wedged bot stays green" scenario described in the audit. → hang detection/heartbeat is mandatory, not optional. The resulting 6h gap must be filled by the 12h REST catch-up on the next start (verify!).
2. **🔴 Whale logger dead since April 18.** Last `whale_data/whale_trades_*.json` = 2026-04-18. Additionally P1.42 confirmed: the last 3 files contain only **49 of 529** symbols.
3. **Whitelist double vocabulary** (detail on P0.4, see C).
4. Junk tables from broken symbol parsing: `BTCUSD1_*`, `BTCU_*`, `ETHU_*` (second-order consequence of P3.3).

---

## C. Evidence per finding

### P0 — confirmed
- **P0.3 Self-Echo ✔:** **109** rows in `orchestrator_suppressed_signals` whose `original_outbox_id` points to the **fleet's own regime trading channel** (-1003963430969). 0 of these were re-opened (cooldown caught them so far) — the loop exists, the crash window remains.
- **P0.4 Whitelist mismatch ✔ (refined):** `bot_regime_whitelist` contains **both** name variants. Pretty names (`MIS1-8h`, `FastInOut`, `5Percent`, `SR`, `VolIndic`, …): `computed_at` = **today 08:06** (the analyzer writes them live). Raw names, which the orchestrator queries (`MIS1-8H`, `Fast In And Out`, `5 Percent`, `Support Resistance`, `Volume Indicator`): `computed_at` = **frozen 2026-04-19**. → The gate "works" (3,043× `wr_below_overall` suppressions), but **for the MIS family + all 5 channel-fallback bots on regime statistics that are 2.5 months stale** (P2.25 in money-relevant form). Fix remains as in Step 1: `pretty_name()` in the orchestrator + stale-row cleanup + `computed_at` staleness gate.
- **P0.7 ✔:** 5 active + 79 closed trades with LONG `target1 <= entry`.
- **P0.9 ✔ (structural):** the PK of the candle tables is `(symbol, open_time)`, live code `6_housekeeping.py:660` uses `ON CONFLICT (open_time)` → every gap insert throws, exception is swallowed. Currently **0 internal 1h gaps across all 529 coins/30d** — the ingestion's 12h REST catch-up carries the system; the nightly safety net still doesn't exist.
- **P0.11 ✔:** UFI1 realizes **25.7% WR (n=35)** vs. the advertised 54.2%/+278R.
- **P0.13 ✔✔ (drastic):** master pkl dummies: `ai_model_*` matches only `ATS1`+`EPD1` out of 22 live model names (rest are `MSI1-*` typos), `conv_bot_*` overlap = **0**. Calibration: **corr(confidence, win) = −0.304**; bucket 0.8–0.9 → 31.1% WR, bucket **0.9–1.0 → 9.3% WR** (n=19,561). The meta-model is **inversely predictive** at its highest confidence — it posts almost only conf>0.85 → the AIM1 channel is actively harmful. Pause/retrain immediately.
- **P0.1 (partially):** `sent_after_retry = 0` → the crash/retry double-send has so far **not** occurred. But: identical messages (md5-equal) multiple times within 60 min in trading channels (FastInOut, VolumeIndicator, PatternDetector, 2-3× each) → upstream double generation (detector refire). Architecture risk remains.

### P1/P2 — confirmed
- **P1.42 ✔:** 49/529 symbols in whale files (cap ~200 streams/conn) + logger dead since 18.4.
- **P2.12 ✔:** stored `rsi_14` == `ewm(span=14)` variant exactly (Δ=0.000), distance to true Wilder RSI avg **4.84 points**.
- **P2.23/#11 ✔:** regime distribution 30d: **TRANSITION 44.5%**, HIGH_VOLA 29.7%, CHOP 25.8%; 2.9 raw switches/day; 17.2% of 2h windows with ≥2 regimes → the fallback path dominates frequently; 256 suppressions via the `regime_is_transition` fallback.
- **P2.27 ✔:** ROM1 SL distance: median 7.9%, **p90=17.9%, max 65.3%**; 20/133 signals >15% → at 20x beyond liquidation.
- **P2.31 ✔:** `targets_hit` up to **21** (EPD1: 215 rows with 20 targets; ROM1/RUB1 double-digit) — the monitor scores far beyond the published TP1-5.
- **P1.12 ✔ (for level values):** old rows (>30d, n=5000): `poc` only 149 distinct, `support_price` 236 distinct (broadcast), `trendline_price` 4997 (per-row ok).
- **P1.40/41 (order of magnitude):** `ml_predictions_master` shadow flood: EPD1 31k + AIM1 25k rows/7d (~72k/week unposted). `pump_dump_events` exists (narrow schema, `spike_time`).
- **P2.9 (historical):** active trades clean (`sl>0` everywhere); `closed_trades_master` contains 162,194 legacy rows with `sl<=0/NULL`.

### Refuted / defused
- **P1.5 ✘:** `current_target_hit` is INTEGER → no `int>str` TypeError possible.
- **P1.26 ✘:** SMC FVG cooldowns exist (SMC_1H/2H/4H/1D_FVG = 83 rows) → the FVG path fires. Dead-code thesis wrong (or applied to an older code version).
- **P1.31/P1.13 ✘ (current):** 0/529 coins without `_1h`/`_1h_indicators`/`_4h_indicators` tables; 0 `ma_200=0` rows (BTC); no internal 1h gaps in 30d.
- **P2.45 (partial aspect):** XAU/XAG/XAUT/PAXG tables exist in full.
- **P2.26 (current):** no stacked OPEN duplicates on coin+direction.
- **P2.38 ✔ cleared:** ABR1 LONG 67.2% / SHORT 59.2% WR (n=110) — no class inversion, `SUCCESS_CLASS_IDX=0` consistent (matches commit d19a68d).

---

## D. Strategy heart-and-kidney check (catalogue #12–14)

**Realized WR from `closed_ai_signals`** (win = ≥TP1; excluding 352k LEGACY rows, which separately show ~49.6% WR):

| Model | n | WR | Calibration (conf→win) |
|---|---|---|---|
| MIS1-72H | 11,822 | 63.9% | **negative** (72%@conf<0.4 → 65%@0.5-0.6) — thresholds meaningless (supports P1.17) |
| MIS1-168H | 7,167 | 58.5% | flat |
| BR1H/2H/4H/1D | 12,034 | 57–60% | — |
| EPD1 | 4,392 | 72.8% | flat (but high baseline level) |
| **ROM1** | 2,677 | **69.2%** | — |
| QM_1H | 3,139 | 67.5% | slightly positive |
| AIM1 | 3,125 | 50.3% | **inverted −0.30** (see P0.13) |
| TD_1H | 2,202 | 57.2% | **positive** (78.5%@conf>0.9) ✓ |
| ATS1 | 1,768 | 65.8% | slightly negative |
| SRA1 | 396 | 69.9% | positive ✓ |
| MIS1-8H | 569 | 52.9% | positive (91%@0.7-0.8, small n) |
| ABR1 | 110 | 63.6% | — |
| **UFI1** | 35 | **25.7%** | → P0.11 |

**Overall 61.1% — ROM1 69.2%**: the orchestrator KPI (#13) is **positive** (+8pp over the fleet average), despite the stale whitelist. Caveat on interpretation: WR without fees/R-weighting, regime close censors foreign trades as neutral (P1.9), monitor targets ≠ Cornix targets (P2.31) — the absolute numbers are optimistically skewed.

**Calibration conclusion (#12):** TD_1H, SRA1, MIS1-8H, QM are genuinely calibrated. MIS1-72H/168H, EPD1, BB flat to negative → forming-candle/feature-skew findings (P1.17-25) empirically supported. AIM1 inverted → P0.13.

---

## E. Regression guard (P2.50) — armed ✔

`extract` against the live DB (24 fixtures: BTC/ETH/SOL/DOGE × 30m/1h/2h/4h/1d/1w) + `refresh` (24 goldens, 111 columns) + `verify` green. Fixtures/golden/manifest committed (this commit).
Note: `python-dotenv` was installed into the live venv (was missing; required by Kythera's `core/config.py`).

## F. Open Step-2 remainder items

- 6h data gap from today (02:25–08:23 UTC): after the fleet restart, check whether the REST catch-up fills it (if not: fix P0.9 first).
- Watchdog double-fleet proof (#8): log shows a clean stop today; historical double starts not systematically evaluated.
- Fee-adjusted PnL (#14) and gap census for 5m/30m timeframes: not computed.
- `bot_unidentified` = 841 suppressions (largest single reason after wr_below_overall) — look at pattern gaps in `identify_bot()`.
