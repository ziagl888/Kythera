# Strategy & model dossiers

**As of:** 2026-07-03 · One dossier per strategy/model family: consolidates all audit layers
(bot engine, model artifacts, trainer, live balance, cross-cutting risks, remediation plan) from
reports 01–17 + AUDIT_TODO. Grades from `16_strategy_concept_evaluation.md` (A = proven edge … F = harmful).
Numbers: deduplicated, unlevered, net −0.10% fee; the monitor-replay caveat (report 17) applies everywhere.

## ML model families

| Dossier | Bot | Grade | Σ net | Key verdict |
|---|---|---|---|---|
| [MIS1](MIS1.md) | 11 | B− (72H) | +24.057 (72H+168H+8H+24H) | Workhorse despite trainer shortcomings; retrain priority #1 |
| [TD/BB-Sniper](TD_BB_Sniper.md) | 25 | B− (TD) | TD +2.387 / BB −524 | TD_1H the only well-calibrated family; BB_1H negative |
| [SRA1](SRA1.md) | 9 | B− | +134 | healthiest pipeline; clarify label semantics |
| [ATS1](ATS1.md) | 12 | C+ | +1.622 | Ranking ok, confidence inverted (OBV skew); short unvalidated |
| [EPD1](EPD1.md) | 10 | C+ | +14.222 | earns strongly, but is queried out-of-distribution |
| [ABR1](ABR1.md) | 18 | C− | +335 | real 7/18 features; no out-of-sample number |
| [RUB1](RUB1.md) | 13 | D+ | +3.675 | ML layer is noise (MACD break); profit = pre-filter+tails |
| [ATB1](ATB1.md) | 14 | D | −172 | scores an event population that was never trained on |
| [QM](QM.md) | 24 | D+ (1H) / F (4H) | −416 | 67% WR, exit gives it all back; stop QM_4H |
| [AIM1](AIM1.md) | 15 | **F** | **−3.399** | inversely predictive — pause |
| [UFI1](UFI1.md) | 29 | **F** | −280 | 25.7% WR, liquidation before SL — stop |

## Classic strategies

| Dossier | Grade | Σ net | Key verdict |
|---|---|---|---|
| [Support Resistance](Support_Resistance.md) | B− | +596 | only net-positive classic (SHORT carries) |
| [Volume Indicator](Volume_Indicator.md) | D+ | −705 | ≈ break-even gross; scoring only 44% replay-consistent |
| [Main Channel](Main_Channel.md) | C− | −77 | RETIRED 2026-07-22 (T-2026-KYT-9050-020) → replaced by MAX2 (SRA2-LONG trade coin-filtered → CH_MAIN) |
| [5 Percent](Five_Percent.md) | D | −5.766 | 71% "WR" and clearly negative |
| [Fast In And Out](Fast_In_And_Out.md) | **F** | **−25.843** | volume without edge; candidate for S11 filter or shutdown |

## Rule-based / SMC / Pattern

| Dossier | Grade | Key verdict |
|---|---|---|
| [SMC Forex/Metals](SMC_Forex_Metals.md) | D− | Forming-candle decisions, weekend refire; unmeasured |
| [Mayank](Mayank.md) | D | missing SL/RR checks |
| [BTC SMC](BTC_SMC.md) | D (**F as-is**) | 100x leverage at 0.4–1.2% SL = liquidation before stop (P0.5) |
| [IP Pattern](IP_Pattern.md) | D | generates the BR family (BR1H/2H/4H/1D): n=11.756, Σ −4.106; BR1H LONG 65.5% vs SHORT 49.5% |

## Meta level

| Dossier | Grade | Key verdict |
|---|---|---|
| [ROM1 / Regime-Orchestrator](ROM1_Orchestrator.md) | C+ | +8pp WR uplift; whitelist 89% default-open — 4-stage plan in report 16 |
| [Market Intelligence](Market_Intelligence.md) | — | Data providers: whale dead since 18.04., funding ok, ticker_10s empty |

**Reading order for remediation:** first the cross-cutting layer (AUDIT_TODO R1–R4, report 17 monitor rewrite,
report 13 X-R1–R6), then dossiers by priority: AIM1/UFI1/BTC_SMC (stop/secure) → MIS1/EPD1/ATS1
(retrain program) → FIFO/5-Percent (filter or shutdown) → orchestrator stage plan.
