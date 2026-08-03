# Dossier: Volume Indicator

> Volume-profile idea with degenerate mechanics and no cooldown — **Note D+** (Report 16) · gross +4,439, **net Σ −705** (fees eat everything) on 51,440 trades. Core verdict: small real kernel (volume zones work), salvageable only with a rebuild — otherwise a pure fee generator.

## 1. Fact sheet
- **Module:** `strategies/strat_volume_indicator.py`, runner `3_detectors.py`, monitoring `5_trade_monitor.py`.
- **Signal logic:** price at a 90d high-volume node (HVN) + 3σ volume spike in the last 5 days determines direction → 30m entry, TP1 +2.5%. Problem: a spike up to 5 days old as a direction signal for a 30m entry is hopelessly stale.
- **Channel:** own Cornix trading channel via `telegram_outbox` (whitelist raw name "Volume Indicator").
- **Cooldowns:** **none** (P1.16) — the only guard is `is_trade_already_active`; since TP1 +2.5% can be hit within an hour, a historical spike event refires for days every 30 min (serial re-entry → 51,440 trades, signal inflation by construction).

## 2. Live balance (Report 14, deduplicated, `closed_trades_master`)
- **n = 51,440** · WR **64.1%** · avg **+0.09%**/trade · median **−0.10%** · net Σ **−705** price-% (gross **+4,439**).
- **Monthly trend:** Feb/May/Jun positive, Mar/Apr **−7.2k** — regime-dependent. Direction split not reported separately.
- With better exit/fee management the strategy would be ≈ break-even (Report 14).
- **Scoring caveat (Report 17):** monitor scoring for Volume Indicator agrees only **44%** with the first-touch replay — the worst value of the classic family, the per-trade scoring is **de facto noise**. Additionally **98 discarded outbox messages** in the Volume Indicator channel (N2).

## 3. Findings
| ID | Severity | One-liner | Status |
|---|---|---|---|
| P1.16 | High | No cooldown — spike up to 5 days old refires every 30 min | ~ ([DB]; Step 2 shows md5-identical messages 2-3x within 60 min in the channel) |
| P2.42 | Medium | Oldest spike wins; spike at index 0 always "Sell"; HVN gate degenerates per tick size (fine ticks: never, coarse: always) | ~ ([DB]) |
| P2.44 | Medium | Volume strat reads 90d×30m (~4,320 rows) per coin per 30-min cycle as the FIRST gate (~2.3M rows/cycle) | ~ |
| P1.15 | High | One bad coin kills the entire detector process | ~ |
| R1/05 | High | Evaluates the still-forming candle; engine timestamps both :02 AND :32 | ✔ (Step 2) |
| 16b | Concept | HVN detection sums volume per exact float close → gate measures tick size instead of volume structure | ✔ (concept assessment) |

## 4. Dependencies & cross-cutting risks
- **R1 forming candle** (proven in Step 2): spike/HVN evaluation on partial candles.
- **R3 TZ mix:** affects Volume Indicator less directly for lack of a cooldown, but the naive timestamps distort every evaluation (replay TZ alignment 50/50).
- **Monitor bugs P1.2/P2.7:** with 44% replay agreement the per-trade ground truth is unusable — whitelist/analyzer statistics on this strategy inherit that.
- **Outbox losses (N2):** 98 of 800 silently discarded messages in the Volume channel; whitelist raw name frozen since 19.04. (P0.4/P2.25).

## 5. Remediation plan
- **Immediate:** P1.16 cooldown (12–24h per coin or dedupe on spike timestamp) — halves the signal inflation with one change; P2.44 guard reordering (HVN cache, reorder gates); P1.15 per-coin isolation.
- **Structural (rebuild per Report 16):** binned HVNs (`pd.cut` + percentile instead of exact float prices), freshness requirement on the spike (iterate backwards, skip i==0 — P2.42), structural targets instead of a fixed +2.5%. Then monitor rewrite + re-score, then reassessment; after that the **S11 filter pattern is transferable** (Report 15: "same pattern afterwards on Volume Indicator (51k trades)"). That a gross profit remains despite everything justifies the rebuild — as the only classic besides Support Resistance with a real kernel.

## 6. Evidence
- `AUDIT_TODO.md`: P1.16, P2.42, P2.44, P1.15, R1, R3
- `audit_reports/05_classic_strats.md`: no-cooldown refire, spike/HVN degeneration, 90d×30m first gate
- `audit_reports/14_bot_performance_db.md` §C: number line incl. gross +4,439, monthly trend
- `audit_reports/16_strategy_concept_evaluation.md` §3: Note D+, rebuild verdict
- `audit_reports/15_strategy_proposals.md`: S11 transferability
- `audit_reports/17_monitor_replay_and_gaps.md` §1–2: agree 44%, 98 outbox losses
- `audit_reports/STEP2_DB_VERIFICATION.md` §C P0.1: md5-identical messages in the Volume channel
