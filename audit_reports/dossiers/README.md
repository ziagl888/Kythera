# Strategie- & Modell-Dossiers

**Stand:** 2026-07-03 · Ein Dossier je Strategie/Modell-Familie: konsolidiert alle Audit-Ebenen
(Bot-Engine, Modell-Artefakte, Trainer, Live-Bilanz, Querschnitts-Risiken, Sanierungsplan) aus den
Reports 01–17 + AUDIT_TODO. Noten aus `16_strategy_concept_evaluation.md` (A = belegte Edge … F = schädlich).
Zahlen: dedupliziert, ungehebelt, netto −0,10% Fee; Vorbehalt Monitor-Replay (Report 17) gilt überall.

## ML-Modell-Familien

| Dossier | Bot | Note | Σ netto | Kernverdikt |
|---|---|---|---|---|
| [MIS1](MIS1.md) | 11 | B− (72H) | +24.057 (72H+168H+8H+24H) | Arbeitspferd trotz Trainer-Mängeln; Retrain-Prio #1 |
| [TD/BB-Sniper](TD_BB_Sniper.md) | 25 | B− (TD) | TD +2.387 / BB −524 | TD_1H einzige gut kalibrierte Familie; BB_1H negativ |
| [SRA1](SRA1.md) | 9 | B− | +134 | gesündeste Pipeline; Label-Semantik klären |
| [ATS1](ATS1.md) | 12 | C+ | +1.622 | Ranking ok, Confidence invers (OBV-Skew); Short unvalidiert |
| [EPD1](EPD1.md) | 10 | C+ | +14.222 | verdient stark, wird aber out-of-distribution befragt |
| [ABR1](ABR1.md) | 18 | C− | +335 | real 7/18 Features; keine Out-of-Sample-Zahl |
| [RUB1](RUB1.md) | 13 | D+ | +3.675 | ML-Layer ist Rauschen (MACD-Bruch); Gewinn = Vorfilter+Tails |
| [ATB1](ATB1.md) | 14 | D | −172 | scored eine nie trainierte Event-Population |
| [QM](QM.md) | 24 | D+ (1H) / F (4H) | −416 | 67% WR, Exit gibt alles zurück; QM_4H stoppen |
| [AIM1](AIM1.md) | 15 | **F** | **−3.399** | invers prädiktiv — pausieren |
| [UFI1](UFI1.md) | 29 | **F** | −280 | 25,7% WR, Liquidation vor SL — stoppen |

## Classic-Strategien

| Dossier | Note | Σ netto | Kernverdikt |
|---|---|---|---|
| [Support Resistance](Support_Resistance.md) | B− | +596 | einzige netto-positive Classic (SHORT trägt) |
| [Volume Indicator](Volume_Indicator.md) | D+ | −705 | ≈ break-even brutto; Scoring nur 44% replay-konsistent |
| [Main Channel](Main_Channel.md) | C− | −77 | Duplikat von Support Resistance — mergen |
| [5 Percent](Five_Percent.md) | D | −5.766 | 71% "WR" und klar negativ |
| [Fast In And Out](Fast_In_And_Out.md) | **F** | **−25.843** | Masse ohne Edge; Kandidat für S11-Filter oder Abschaltung |

## Rule-based / SMC / Pattern

| Dossier | Note | Kernverdikt |
|---|---|---|
| [SMC Forex/Metals](SMC_Forex_Metals.md) | D− | Forming-Candle-Entscheidungen, Weekend-Refire; unvermessen |
| [Mayank](Mayank.md) | D | fehlende SL/RR-Checks |
| [BTC SMC](BTC_SMC.md) | D (**F as-is**) | 100x Leverage bei 0,4–1,2% SL = Liquidation vor Stop (P0.5) |
| [IP Pattern](IP_Pattern.md) | D | erzeugt die BR-Familie (BR1H/2H/4H/1D): n=11.756, Σ −4.106; BR1H LONG 65,5% vs SHORT 49,5% |

## Meta-Ebene

| Dossier | Note | Kernverdikt |
|---|---|---|
| [ROM1 / Regime-Orchestrator](ROM1_Orchestrator.md) | C+ | +8pp WR-Mehrwert; Whitelist 89% default-open — 4-Stufen-Plan in Report 16 |
| [Market Intelligence](Market_Intelligence.md) | — | Datenlieferanten: Whale tot seit 18.04., Funding ok, ticker_10s leer |

**Lesereihenfolge für die Sanierung:** zuerst Querschnitt (AUDIT_TODO R1–R4, Report 17 Monitor-Rewrite,
Report 13 X-R1–R6), dann Dossiers nach Priorität: AIM1/UFI1/BTC_SMC (stoppen/sichern) → MIS1/EPD1/ATS1
(Retrain-Programm) → FIFO/5-Percent (Filter oder Abschaltung) → Orchestrator-Stufenplan.
