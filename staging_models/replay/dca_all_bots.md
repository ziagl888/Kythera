# DCA effect fleet-wide — A (DCA) vs B (single-entry1) vs C (SL@entry2) — T-2026-KYT-9050-045

_generated 2026-07-26 06:53:23.300224+00:00 · read-only · 24 bots · per max outbox window · until 2026-07-27 00:00:00_

**Why per-bot window:** `closed_ai_signals` has no entry2 → its realized numbers are already ≈ **arm B** (single entry1, without DCA). The DCA effect (arm A) lives only in the immutable Cornix text (`telegram_outbox`), whose per-bot retention limits every window; entry2/SL are **not** fixed %-spacings → not reconstructable. Metric risk-adjusted (T-041): per-trade leveraged **Sharpe** + compounding **MaxDD (2%)**. **DCA drag = B−A** (Sharpe; positive → dropping DCA helps).

| Bot | Window from | n | **A Sharpe/MaxDD** | **B Sharpe/MaxDD** | **C Sharpe/MaxDD** | **DCA drag (B−A)** | C−B | Verdict |
|---|---|--:|--:|--:|--:|--:|--:|---|
| EPD2 | 2026-07-11 | 8 | -0.769/7.9% | -0.584/8.9% | -0.493/7.6% | **+0.185** | +0.091 | THIN |
| MIS2-8H | 2026-07-08 | 62 | 0.24/13.7% | 0.358/14.9% | 0.175/16.2% | **+0.118** | -0.183 | DCA-HURTS |
| RUB2 | 2026-07-11 | 140 | 0.188/7.0% | 0.295/7.5% | 0.205/8.4% | **+0.107** | -0.09 | DCA-HURTS |
| TD_1H | 2026-04-17 | 34 | 0.15/5.4% | 0.246/6.3% | 0.269/4.3% | **+0.096** | +0.023 | DCA-HURTS |
| AIM2 | 2026-07-11 | 487 | 0.198/9.5% | 0.294/10.0% | 0.275/9.8% | **+0.096** | -0.019 | DCA-HURTS |
| MIS2-72H | 2026-07-19 | 39 | 0.281/8.1% | 0.373/7.8% | 0.164/9.4% | **+0.092** | -0.209 | DCA-HURTS |
| MIS1-72H | 2026-07-04 | 317 | 0.12/19.4% | 0.194/22.0% | 0.203/16.6% | **+0.074** | +0.009 | DCA-HURTS |
| MIS1-168H | 2026-05-14 | 367 | 0.109/24.4% | 0.183/20.2% | 0.194/17.4% | **+0.074** | +0.011 | DCA-HURTS |
| ABR2 | 2026-07-19 | 46 | 0.062/5.2% | 0.135/6.0% | 0.131/3.9% | **+0.073** | -0.004 | DCA-HURTS |
| SRA2 | 2026-07-21 | 114 | 0.019/11.6% | 0.092/14.1% | 0.026/14.4% | **+0.073** | -0.066 | DCA-HURTS |
| MIS2-168H | 2026-07-19 | 21 | 0.039/9.0% | 0.107/9.6% | -0.143/11.7% | **+0.068** | -0.25 | THIN |
| EPD3 | 2026-07-21 | 1148 | 0.144/10.6% | 0.204/11.9% | 0.082/24.5% | **+0.06** | -0.122 | DCA-HURTS |
| MAX1 | 2026-07-16 | 107 | 0.131/9.4% | 0.19/13.8% | 0.101/15.7% | **+0.059** | -0.089 | DCA-HURTS |
| ATS2 | 2026-07-25 | 24 | 0.919/0.8% | 0.977/1.3% | 1.033/1.0% | **+0.058** | +0.056 | THIN |
| BB_4H | 2026-07-04 | 53 | 0.197/5.8% | 0.243/5.9% | 0.15/6.6% | **+0.046** | -0.093 | DCA-HURTS |
| BR4H | 2026-07-13 | 125 | 0.04/21.0% | 0.083/24.1% | 0.076/17.3% | **+0.043** | -0.007 | DCA-HURTS |
| SRA1 | 2026-07-19 | 61 | 0.111/6.0% | 0.152/9.8% | 0.007/15.2% | **+0.041** | -0.145 | DCA-HURTS |
| MIS2-24H | 2026-07-19 | 26 | 0.303/9.7% | 0.331/10.1% | 0.112/12.9% | **+0.028** | -0.219 | THIN |
| BR2H | 2026-07-06 | 334 | -0.013/42.2% | 0.014/45.8% | 0.038/24.8% | **+0.027** | +0.024 | DCA-HURTS |
| ROM1 | 2026-05-14 | 2507 | 0.16/10.2% | 0.174/14.5% | 0.052/46.2% | **+0.014** | -0.122 | neutral |
| BR1Hv2 | 2026-07-16 | 580 | 0.039/49.3% | 0.043/61.7% | 0.006/51.6% | **+0.004** | -0.037 | neutral |
| TD_4H | 2026-04-17 | 14 | -0.29/6.4% | -0.287/9.4% | -0.27/8.0% | **+0.003** | +0.017 | THIN |
| BR1D | 2026-07-20 | 7 | -0.902/5.6% | -1.188/8.1% | -2.087/5.3% | **-0.286** | -0.899 | THIN |
| BR1H | 2026-07-16 | 0 | — | — | — | **—** | — | THIN |

### Fleet verdict

- **DCA hurts on 15/17 solid bots** (n≥30, DCA drag B−A > +0.02). Median drag +0.073.
- **entry2-as-SL (C) beats B on 2/17** — bot-type-dependent (trend vs mean-reversion).

**Honest limits:** per bot, only as far as the outbox geometry reaches (mostly ~2-3 weeks; ROM1/MIS1-168H ~2.5 months). entry2-present set, compounding sequential-after-close. Dropping DCA = its own deploy candidate (Michi decision). NO-EDGE/no-effect is valid.
