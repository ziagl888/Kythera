# Wave-Buildup — Per-Bot Trailing-Close Ranking (Phase B, T-2026-KYT-9050-041)

_generated 2026-07-25 17:57:13.883672+00:00 · read-only · ALL bots · 20x assumed · tf 15m · from 2026-03-01 · n_trades 91547_

**Question:** for which bots does a tight **trailing close** lift the risk-adjusted return? Per bot (bot_catalog family): per-trade leveraged **Sharpe** hold vs trailing 10% vs best-X, and the compounding **MaxDD** (fixed 2% fraction) hold vs best-X. Sorted by **Sharpe uplift (best − hold)**. Methodology + caveats as in Phase A (first-order, entry1-only, 20x, trigger optimism). LEGACY status + Feb backfill excluded (from 2026-03-01).

| Bot | tags | n | WR% | avg giveback | **Sharpe hold** | Sharpe t10% | best-X | **Sharpe best** | **uplift** | MaxDD hold→best | verdict |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|
| 33_ai_fif1_bot | FIF1 | 139 | 64.0 | +25.0 | **-0.022** | +0.311 | 10% | **+0.311** | **+0.333** | 9.3%→2.3% | TRAILING-HELPS |
| 24_quasimodo_bot | QM_1H,QM_4H | 3621 | 34.9 | +48.3 | **+0.074** | +0.403 | 10% | **+0.403** | **+0.329** | 44.9%→10.1% | TRAILING-HELPS |
| 14_ai_atb_bot | ATB1,ATB2 | 329 | 45.0 | +95.2 | **+0.151** | +0.413 | 10% | **+0.413** | **+0.262** | 26.3%→5.9% | TRAILING-HELPS |
| 15_ai_master_bot | AIM1,AIM2 | 5527 | 36.5 | +164.7 | **+0.225** | +0.476 | 10% | **+0.476** | **+0.251** | 56.9%→7.6% | TRAILING-HELPS |
| 28_signal_orchestrator | ROM1 | 10136 | 44.4 | +78.1 | **+0.147** | +0.354 | 10% | **+0.354** | **+0.208** | 63.2%→8.7% | TRAILING-HELPS |
| 13_ai_rub_bot | RUB1,RUB2,RUB3 | 3016 | 41.1 | +159.8 | **+0.291** | +0.463 | 10% | **+0.463** | **+0.172** | 45.1%→12.7% | TRAILING-HELPS |
| 7_pattern_detector | BR1D,BR1H,BR1Hv2,BR2H,BR4H | 15278 | 33.4 | +116.1 | **+0.093** | +0.248 | 10% | **+0.248** | **+0.156** | 99.9%→35.1% | TRAILING-HELPS |
| 25_smc_ml_sniper | BB2_4H,BB_1H,BB_4H,TD2_4H,TD_1H,TD_4H | 10599 | 35.3 | +128.9 | **+0.176** | +0.331 | 10% | **+0.331** | **+0.155** | 79.0%→6.3% | TRAILING-HELPS |
| 18_ai_abr1_bot | ABR1,ABR2 | 326 | 37.1 | +147.6 | **+0.164** | +0.309 | 10% | **+0.309** | **+0.145** | 37.5%→5.0% | TRAILING-HELPS |
| 9_ai_sr_bot | SRA1,SRA2 | 1428 | 52.3 | +65.4 | **+0.2** | +0.332 | 10% | **+0.332** | **+0.132** | 28.4%→12.1% | TRAILING-HELPS |
| 37_ai_tsm1_bot | TSM1 | 656 | 52.3 | +43.2 | **+0.138** | +0.258 | 10% | **+0.258** | **+0.12** | 31.1%→5.6% | TRAILING-HELPS |
| 11_ai_mis_bot | MIS1-168h,MIS1-24h,MIS1-72h,MIS1-8h,MIS2-168h,MIS2-24h,MIS2-72h,MIS2-8h | 21822 | 41.7 | +152.5 | **+0.256** | +0.374 | 10% | **+0.374** | **+0.117** | 100.0%→10.4% | TRAILING-HELPS |
| 10_pump_dump_detector | EPD1,EPD2,EPD3 | 12764 | 51.1 | +106.7 | **+0.271** | +0.388 | 10% | **+0.388** | **+0.116** | 83.1%→7.4% | TRAILING-HELPS |
| 12_ai_ats_bot | ATS1,ATS2 | 3358 | 46.8 | +113.4 | **+0.206** | +0.265 | 10% | **+0.265** | **+0.06** | 69.7%→21.6% | TRAILING-HELPS |
| 38_ai_skw1_bot | SKW1 | 60 | 56.7 | +72.2 | **+0.304** | +0.357 | 10% | **+0.357** | **+0.053** | 8.7%→2.0% | TRAILING-HELPS |
| 39_ai_xsm1_bot | XSM1,XSR1 | 63 | 52.4 | +71.5 | **+0.321** | +0.356 | 10% | **+0.356** | **+0.034** | 4.0%→2.0% | neutral/no |
| 29_ufi1_bot | UFI1 | 55 | 45.5 | +240.4 | **+0.574** | +0.544 | 10% | **+0.544** | **-0.029** | 8.2%→2.0% | neutral/no |
| 34_ai_max1_bot | MAX1,MAX2 | 159 | 57.9 | +34.1 | **+0.315** | +0.271 | 10% | **+0.271** | **-0.044** | 4.7%→3.1% | neutral/no |

**Reading aid:** positive **uplift** = trailing lifts the per-trade Sharpe (the Phase-A reversal holds for this bot). MaxDD hold→best shows the drawdown reduction. `THIN` (n<30) = sign not reliable. Finalists (high uplift, solid n) belong on the T-035 high-fidelity harness (5m + 10s + DCA-faithful) for confirmation.

## Honest limitations

- First-order wave: recorded entry/close as ground truth, NO intra-trade DCA/TP laddering — the unrealized amplitude is slightly overestimated (peak timing unaffected). The T-035 Phase-2 harness is the laddering-faithful reference (but only ~7d outbox window there).
- Leverage flat 20x (not persisted before ~July). The pattern (giveback, Sharpe reversal) is leverage-agnostic; the absolute leveraged numbers scale with this assumption + the -100% clamp.
- Trailing sim: 1h wick, peak-before-trigger on the same candle (slightly optimistic at the trigger level). A 5m/10s resolver (T-035) tightens this; the DIRECTION (Sharpe/MaxDD advantage) is robust.
- Compounding sequential after close → ignores concurrency (up to the concurrently open trades mentioned above, cross-margin). The absolute multiples (×1e26) are NOT literal — ratio + MaxDD are the signal.
- Live/shadow gate state is time-variable; no gate filtering here — these are the AIM/SRA strategies across all generations (AIM1→AIM2, SRA1→SRA2).
