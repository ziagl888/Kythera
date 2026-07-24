# Bot-Varianten-Index (auto-generiert)

> Generiert von `tools/bot_variants/index.py` (T-2026-KYT-9050-038). **Nicht von Hand editieren** — regenerieren mit `python -m tools.bot_variants.index --write`.
>
> Join über `core.bot_catalog` (Tag→Family/Script) · `core.shadow_gate` (Lifecycle je (Tag,Richtung) + SHADOW_ARTIFACTS) · Artefakt-meta · Dateisystem (root/staging/archive) · git. Deterministisch/idempotent.

**Generationen:** 48 · **geteilte Dateinamen:** 2 · **unklassifizierte Artefakte:** 6 · **unbekannte Tags:** 0

`code_ref` in Phase 1 konservativ: `HEAD` wenn die Generation live/aktiv ist, sonst leer (exakte git-SHA je Alt-Generation folgt in Phase 2 / D4).

## Generationen

| Family | Tag | Script | Lifecycle | Artefakte (Richtung:Datei@Ort#md5) | model_id | code_ref | Provenienz |
|---|---|---|---|---|---|---|---|
| ABR | `ABR2` | 18_ai_abr1_bot.py | LONG:shadow, SHORT:shadow | LONG:`bt2_model_LONG.json`@root#a25ba51b<br>SHORT:`bt2_model_SHORT.json`@root#4d56f667 | ABR2 | — | Break&Retest binary + Funding-Gate (bot 18) |
| AIM | `AIM1` | 15_ai_master_bot.py | LONG:retired, SHORT:retired | —<br>_regelbasiert / kein Modell-Artefakt_ | — | — | Master-Ranker/Gate über Kandidaten (bot 15) |
| AIM | `AIM2` | 15_ai_master_bot.py | LONG:live, SHORT:live | LONG:`master_meta_model_aim2.pkl`@root#5ef8df53<br>SHORT:`master_meta_model_aim2.pkl`@root#5ef8df53 | — | HEAD | Master-Ranker/Gate über Kandidaten (bot 15) |
| AIM | `AIM2-TOPN` | 15_ai_master_bot.py | LONG:retired, SHORT:retired | —<br>_regelbasiert / kein Modell-Artefakt_ | — | — | High-Conviction-Top-N-Kanal über AIM2; retired T-037 |
| ATB | `ATB1` | 14_ai_atb_bot.py | LONG:silent, SHORT:silent | —<br>_regelbasiert / kein Modell-Artefakt_ | — | — | Converging-Channel Break (bot 14); ATB2-Neuaufbau |
| ATB | `ATB2` | 14_ai_atb_bot.py | LONG:shadow, SHORT:shadow | LONG:`atb2_model_LONG.pkl`@staging#3fb8a0f3<br>SHORT:`atb2_model_SHORT.pkl`@staging#3d27c650 | ATB2 | — | Converging-Channel Break (bot 14); ATB2-Neuaufbau |
| ATS | `ATS1` | 12_ai_ats_bot.py | LONG:silent, SHORT:silent | —<br>_regelbasiert / kein Modell-Artefakt_ | — | — | Trend-Strength-Sniper TSI (bot 12) |
| ATS | `ATS1_ROBUST` | 12_ai_ats_bot.py | LONG:retired, SHORT:retired | LONG:`model_tsi_long_robust.pkl`@root#73ea915e<br>SHORT:`model_tsi_short_robust.pkl`@root#cf090013 | — | — | ATS1_Robust Legacy (model_tsi_*_robust.pkl); ATS2 ist der Nachfolger |
| ATS | `ATS2` | 12_ai_ats_bot.py | LONG:live, SHORT:live | LONG:`ats2_model_LONG.pkl`@staging#5d27eca5<br>SHORT:`ats2_model_SHORT.pkl`@staging#121b548a | ATS2 | HEAD | Trend-Strength-Sniper TSI (bot 12) |
| BB | `BB2_4H` | 25_smc_ml_sniper.py | LONG:shadow, SHORT:shadow | —<br>_regelbasiert / kein Modell-Artefakt_ | — | — | SMC-ML-Sniper Break (bot 25) |
| BB | `BB_1H` | 25_smc_ml_sniper.py | LONG:live, SHORT:shadow | LONG:`bb_xgboost_model_1h.pkl`@root#19094767<br>SHORT:`bb_xgboost_model_1h.pkl`@root#19094767 | — | HEAD | SMC-ML-Sniper Break (bot 25) |
| BB | `BB_4H` | 25_smc_ml_sniper.py | LONG:live, SHORT:shadow | LONG:`bb_xgboost_model_4h.pkl`@root#a0c3117a<br>SHORT:`bb_xgboost_model_4h.pkl`@root#a0c3117a | — | HEAD | SMC-ML-Sniper Break (bot 25) |
| BR | `BR1D` | 7_pattern_detector.py | LONG:shadow, SHORT:shadow | —<br>_regelbasiert / kein Modell-Artefakt_ | — | — | Pattern-Breakout-Detector (bot 7) |
| BR | `BR1H` | 7_pattern_detector.py | SHORT:shadow | —<br>_regelbasiert / kein Modell-Artefakt_ | — | — | Pattern-Breakout-Detector (bot 7) |
| BR | `BR1HV2` | 7_pattern_detector.py | LONG:shadow, SHORT:shadow | —<br>_regelbasiert / kein Modell-Artefakt_ | — | — | Pattern-Breakout-Detector (bot 7) |
| BR | `BR2H` | 7_pattern_detector.py | SHORT:shadow | —<br>_regelbasiert / kein Modell-Artefakt_ | — | — | Pattern-Breakout-Detector (bot 7) |
| BR | `BR4H` | 7_pattern_detector.py | SHORT:shadow | —<br>_regelbasiert / kein Modell-Artefakt_ | — | — | Pattern-Breakout-Detector (bot 7) |
| EPD | `EPD1` | 10_pump_dump_detector.py | LONG:shadow | —<br>_regelbasiert / kein Modell-Artefakt_ | — | — | Pump/Dump-Detector (bot 10); EPD2=EPD_LEGACY_TAG |
| EPD | `EPD2` | 10_pump_dump_detector.py | LONG:shadow, SHORT:shadow | LONG:`epd2_model_LONG.pkl`@staging#2702a57b<br>LONG:`pump_dump_model.pkl`@root#6c09741a<br>SHORT:`epd2_model_SHORT.pkl`@MISSING#—<br>SHORT:`pump_dump_model.pkl`@root#6c09741a<br>_Artefakt fehlt auf Platte: epd2_model_SHORT.pkl_ | EPD2 | — | Pump/Dump-Detector (bot 10); EPD2=EPD_LEGACY_TAG |
| EPD | `EPD3` | 10_pump_dump_detector.py | LONG:shadow, SHORT:shadow | LONG:`epd2_model_LONG.pkl`@staging#2702a57b<br>SHORT:`epd3_model_SHORT.pkl`@root#e0f7bfb3 | EPD2 | — | EPD2-Retrain-Challenger; LONG teilt epd2_model_LONG.pkl mit EPD2 |
| FIF | `FIF1` | 33_ai_fif1_bot.py | LONG:shadow, SHORT:shadow | LONG:`fif1_model.pkl`@root#c95b6194<br>SHORT:`fif1_model.pkl`@root#c95b6194 | FIF1 | — | First-In-First-Out (bot 33) |
| FMR | `FMR2` | 31_ai_fmr1_bot.py | LONG:shadow, SHORT:shadow | LONG:`fmr2_model.pkl`@staging#576bc339<br>SHORT:`fmr2_model.pkl`@staging#576bc339 | FMR2 | — | Funding-Mean-Reversion-Exit (bot 31) |
| LIS | `LIS1` | 36_ai_lis1_bot.py | SHORT:shadow | —<br>_regelbasiert / kein Modell-Artefakt_ | — | — | Post-Listing-Drift-Fade (bot 36) |
| MAX | `MAX1` | 34_ai_max1_bot.py | SHORT:live | SHORT:`max1_model_SHORT.pkl`@root#e33b6df8 | MAX1 | HEAD | MAX1 (bot 34) / MAX2 SRA2-LONG-Fork (bot 9) |
| MAX | `MAX2` | 34_ai_max1_bot.py | LONG:live | —<br>_regelbasiert / kein Modell-Artefakt_ | — | HEAD | kein Modell — SRA2-LONG-Fork nach CH_MAIN (bot 9) |
| MIS | `MIS1-168H` | 11_ai_mis_bot.py | LONG:live, SHORT:shadow | LONG:`pump_model_168h_pump_final.pkl`@root#7bfa9049<br>SHORT:`pump_model_168h_dump_final.pkl`@root#0e1fb917 | — | HEAD | Momentum-Impuls-Spike pump/dump (bot 11); MIS1 revived T-034 |
| MIS | `MIS1-24H` | 11_ai_mis_bot.py | LONG:live, SHORT:shadow | LONG:`pump_model_24h_pump_final.pkl`@root#65eca3c0<br>SHORT:`pump_model_24h_dump_final.pkl`@root#371c3c81 | — | HEAD | Momentum-Impuls-Spike pump/dump (bot 11); MIS1 revived T-034 |
| MIS | `MIS1-72H` | 11_ai_mis_bot.py | LONG:live, SHORT:shadow | LONG:`pump_model_72h_pump_final.pkl`@root#d2aee548<br>SHORT:`pump_model_72h_dump_final.pkl`@root#440244f8 | — | HEAD | Momentum-Impuls-Spike pump/dump (bot 11); MIS1 revived T-034 |
| MIS | `MIS1-8H` | 11_ai_mis_bot.py | LONG:shadow, SHORT:live | LONG:`pump_model_8h_pump_final.pkl`@root#23d59d70<br>SHORT:`pump_model_8h_dump_final.pkl`@root#aefc8e37 | — | HEAD | Momentum-Impuls-Spike pump/dump (bot 11); MIS1 revived T-034 |
| MIS | `MIS2-168H` | 11_ai_mis_bot.py | LONG:shadow, SHORT:live | LONG:`mis2_model_168h_pump.pkl`@root#67527574<br>SHORT:`mis2_model_168h_dump.pkl`@root#cd6b461f | MIS2 | HEAD | Momentum-Impuls-Spike pump/dump (bot 11); MIS1 revived T-034 |
| MIS | `MIS2-24H` | 11_ai_mis_bot.py | LONG:shadow, SHORT:live | LONG:`mis2_model_24h_pump.pkl`@root#2f220811<br>SHORT:`mis2_model_24h_dump.pkl`@root#cc38ff4d | MIS2 | HEAD | Momentum-Impuls-Spike pump/dump (bot 11); MIS1 revived T-034 |
| MIS | `MIS2-72H` | 11_ai_mis_bot.py | LONG:shadow, SHORT:live | LONG:`mis2_model_72h_pump.pkl`@root#859064c9<br>SHORT:`mis2_model_72h_dump.pkl`@root#08c87fc5 | MIS2 | HEAD | Momentum-Impuls-Spike pump/dump (bot 11); MIS1 revived T-034 |
| MIS | `MIS2-8H` | 11_ai_mis_bot.py | LONG:shadow, SHORT:shadow | LONG:`mis2_model_8h_pump.pkl`@root#7eb5fc64<br>SHORT:`mis2_model_8h_dump.pkl`@root#7e51f58f | MIS2 | — | Momentum-Impuls-Spike pump/dump (bot 11); MIS1 revived T-034 |
| MIS | `MSI1` | 11_ai_mis_bot.py | LONG:retired, SHORT:retired | —<br>_regelbasiert / kein Modell-Artefakt_ | — | — | Momentum-Impuls-Spike pump/dump (bot 11); MIS1 revived T-034 |
| PEX | `PEX1` | 30_ai_pex1_bot.py | LONG:live, SHORT:live | LONG:`pex1_model.pkl`@root#70a5bf63<br>SHORT:`pex1_model.pkl`@root#70a5bf63 | PEX1 | HEAD | Price-Extension (bot 30) |
| QM | `QM_1H` | 24_quasimodo_bot.py | LONG:live, SHORT:shadow | LONG:`qm_xgboost_model_1h.pkl`@root#c217d694<br>SHORT:`qm_xgboost_model_1h.pkl`@root#c217d694 | — | HEAD | Quasimodo-Pattern (bot 24) |
| QM | `QM_4H` | 24_quasimodo_bot.py | LONG:live, SHORT:shadow | LONG:`qm_xgboost_model_4h.pkl`@root#8759d9ee<br>SHORT:`qm_xgboost_model_4h.pkl`@root#8759d9ee | — | HEAD | Quasimodo-Pattern (bot 24) |
| ROM | `ROM1` | 28_signal_orchestrator.py | LONG:live, SHORT:live | —<br>_regelbasiert / kein Modell-Artefakt_ | — | HEAD | Regime-Orchestrator Re-Forwarder (bot 28) |
| RUB | `RUB1` | 13_ai_rub_bot.py | LONG:live, SHORT:live | LONG:`long_reversion_model.joblib`@root#0227bb4a<br>SHORT:`short_reversion_model.joblib`@root#16ca3711 | — | HEAD | Rubberband HVN/S-R-Reversion (bot 13); RUB1 revived T-037 |
| RUB | `RUB2` | 13_ai_rub_bot.py | LONG:shadow, SHORT:shadow | LONG:`rub2_model_LONG.pkl`@staging#162ddb95<br>SHORT:`rub2_model_SHORT.pkl`@root#24fb499c | RUB2 | — | Rubberband HVN/S-R-Reversion (bot 13); RUB1 revived T-037 |
| RUB | `RUB3` | 13_ai_rub_bot.py | LONG:shadow | LONG:`rub2_model_LONG.pkl`@staging#162ddb95 | RUB2 | — | rub2_model_LONG-Challenger vs. live RUB1-LONG |
| RUB | `RUB4` | 13_ai_rub_bot.py | LONG:shadow | —<br>_regelbasiert / kein Modell-Artefakt_ | — | — | funding-gegatetes RUB3 (fund_24h>+3bps); nutzt RUB3-Artefakt |
| SRA | `SRA1` | 9_ai_sr_bot.py | LONG:shadow, SHORT:shadow | —<br>_regelbasiert / kein Modell-Artefakt_ | — | — | Support/Resistance-AI (bot 9) |
| SRA | `SRA2` | 9_ai_sr_bot.py | LONG:live, SHORT:live | LONG:`sra2_model_LONG.json`@root#a51bd5bc<br>SHORT:`sra2_model_SHORT.json`@staging#ce4ce83c | SRA2 | HEAD | Support/Resistance-AI (bot 9) |
| TD | `TD_1H` | 25_smc_ml_sniper.py | LONG:live, SHORT:live | LONG:`td_xgboost_model_1h.pkl`@root#5607f164<br>SHORT:`td_xgboost_model_1h.pkl`@root#5607f164 | — | HEAD | SMC-ML-Sniper Trend-Detect (bot 25) |
| TD | `TD_4H` | 25_smc_ml_sniper.py | LONG:live, SHORT:live | LONG:`td_xgboost_model_4h.pkl`@root#e6065bcb<br>SHORT:`td_xgboost_model_4h.pkl`@root#e6065bcb | — | HEAD | SMC-ML-Sniper Trend-Detect (bot 25) |
| TRM | `TRM1` | 32_ai_trm1_bot.py | LONG:live, SHORT:live | —<br>_regelbasiert / kein Modell-Artefakt_ | — | HEAD | TRM1 (bot 32) |
| UFI | `UFI1` | 29_ufi1_bot.py | LONG:live, SHORT:live | —<br>_regelbasiert / kein Modell-Artefakt_ | — | HEAD | UFI1 (bot 29) |

## Geteilte Dateinamen (Kollisions-Hazard)

| Datei | Tags | Ort |
|---|---|---|
| `epd2_model_LONG.pkl` | EPD2, EPD3 | staging |
| `rub2_model_LONG.pkl` | RUB2, RUB3 | staging |

## Unklassifizierte Artefakte

_Modell-artige Dateien ohne Generations-Zuordnung — Operator prüfen:_

| Datei | Ort | md5 |
|---|---|---|
| `long_trend_prediction_model.joblib` | root | 7870b61e |
| `master_trade_model_xgboost_combined_signals.pkl` | root | f43b4212 |
| `qm_xgboost_model_v2.pkl` | root | 90b4b9db |
| `short_trend_prediction_model.joblib` | root | 024605cd |
| `trade_success_xgb_LONG_v2.json` | root | eadb7640 |
| `trade_success_xgb_SHORT_v2.json` | root | 5d9d1962 |

## Unbekannte Tags (kein Fleet-Script)

_keine_
