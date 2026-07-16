# K6 · BRD — market breadth/dispersion study (T-2026-CU-9050-140)

_Generated 2026-07-16T20:47:53.296384+00:00 · read-only · status=complete · coins_loaded=527/530 · RUB-LONG events=21604 · peak RSS 187.1 MB_

**VERDICT: weak/mixed-breadth-signal (not deployable)**

- RUB-LONG head-to-head (win-logit, chrono test): ΔAUC breadth-over-BTC-regime = **0.0418** (≥0.02 required: True)
- OOS sign+magnitude-stable breadth features (|ρ_test|≥0.03, ≥2 needed for a clean edge): **2** — brd_adv_decline_ratio, total3_vw_dist_reg90d; features that sign-FLIP val→test: 6/11
- regime_history TREND_UP incremental ΔAUC (independent OOS check): -0.1471 — contradicts breadth: True

> The multivariate head-to-head lift is modest (+0.0418 test AUC) but NOT corroborated: only 2 of the 11 features are OOS sign+magnitude stable (6 sign-flip val→test), and the independent regime_history TREND_UP test shows breadth HURTING OOS (Δ=-0.1471). Two OOS tests disagree ⇒ no clean, robust edge — §K6 near-no-op. The shared builder stays as infrastructure (HMM T-020, whitelist §23); no RUB-LONG breadth gate is licensed.

Breadth is a PRICE proxy over active USDT-perps (survivorship-biased); TOTAL3 has no real market-cap weights — see core/breadth_features docstring.

## Builder output — daily breadth panel

- panel rows (days): 873 · span (UTC): 2024-02-24 00:00:00+00:00 .. 2026-07-15 00:00:00+00:00
- features emitted: brd_pct_above_ema200, brd_pct_above_ema50, brd_median_ret_7d, brd_adv_decline_ratio, brd_dispersion_vs_btc, total3_ew_level, total3_ew_dist_reg90d, total3_ew_breakout, total3_vw_level, total3_vw_dist_reg90d, total3_vw_breakout

## (a) RUB-LONG events vs breadth as-of

- RUB LONG events: 21604 (with as-of breadth: 21604)
- overall: avg net PnL -0.6244% · WR 0.4507

### Head-to-head win-logit (RUB-LONG win; chrono 70/30, n_test=3641, overlap n=12134)

- AUC BTC-regime only = 0.5799 → BTC-regime + breadth = 0.6218 (Δ=0.0418)

### Per-feature gradient (Spearman vs net_pnl_pct; sign must survive the chrono split)

| feature | Spearman all | val | test |
|---|--:|--:|--:|
| brd_pct_above_ema200 | -0.0911 | 0.0485 | -0.1926 |
| brd_pct_above_ema50 | 0.0007 | 0.0627 | -0.1281 |
| brd_median_ret_7d | 0.0216 | 0.0408 | -0.0403 |
| brd_adv_decline_ratio | -0.0245 | -0.0105 | -0.0605 |
| brd_dispersion_vs_btc | -0.0085 | -0.0276 | -0.0204 |
| total3_ew_level | -0.1316 | 0.0748 | -0.2242 |
| total3_ew_dist_reg90d | 0.0026 | 0.0628 | 0.0119 |
| total3_ew_breakout | 0.0002 | 0.0019 | -0.0092 |
| total3_vw_level | 0.0818 | -0.1214 | -0.0181 |
| total3_vw_dist_reg90d | -0.0821 | -0.0533 | -0.0407 |
| total3_vw_breakout | 0.024 | 0.055 | -0.0111 |

### Top vs bottom tercile net-PnL expectancy (with chrono val/test)

| feature | bottom n | bottom PnL% | bottom test% | top n | top PnL% | top test% |
|---|--:|--:|--:|--:|--:|--:|
| brd_pct_above_ema200 | 7253 | 1.2215 | 2.4077 | 7291 | -1.6474 | -2.8502 |
| brd_pct_above_ema50 | 7253 | -0.5 | 2.4866 | 7204 | -0.8185 | -1.0835 |
| brd_median_ret_7d | 7477 | -0.9655 | 2.2462 | 7221 | -1.05 | -0.1503 |
| brd_adv_decline_ratio | 7252 | 0.03 | 1.6531 | 7229 | -1.0969 | -0.6793 |
| brd_dispersion_vs_btc | 7235 | -1.0949 | 0.3012 | 7406 | -1.4301 | -0.3513 |
| total3_ew_level | 7367 | 1.8248 | 2.5311 | 7229 | -2.1891 | None |
| total3_ew_dist_reg90d | 7540 | -0.651 | 0.7956 | 7204 | -0.1657 | 1.104 |
| total3_ew_breakout | 21499 | -0.6218 | 0.8957 | 21604 | -0.6244 | 0.8817 |
| total3_vw_level | 7217 | -1.9434 | None | 7209 | 0.1226 | 0.1226 |
| total3_vw_dist_reg90d | 7211 | 1.7685 | 1.8418 | 7585 | -1.4248 | -0.1701 |
| total3_vw_breakout | 9589 | -0.8167 | 1.1223 | 12015 | -0.4708 | 0.6801 |

### RUB-LONG month-split (overall)

| month | n | avg net PnL% | WR |
|---|--:|--:|--:|
| 2025-07 | 1054 | -3.8403 | 0.1898 |
| 2025-08 | 1191 | 3.5049 | 0.5609 |
| 2025-09 | 1398 | 1.9834 | 0.5579 |
| 2025-10 | 2506 | -4.2091 | 0.3839 |
| 2025-11 | 1870 | -5.8864 | 0.3048 |
| 2025-12 | 1269 | 1.9802 | 0.5217 |
| 2026-01 | 880 | -3.6555 | 0.2591 |
| 2026-02 | 3778 | 0.7322 | 0.4505 |
| 2026-03 | 1063 | 1.9618 | 0.5456 |
| 2026-04 | 529 | 3.0868 | 0.6181 |
| 2026-05 | 1870 | -1.0272 | 0.5406 |
| 2026-06 | 3606 | 0.2297 | 0.4762 |
| 2026-07 | 590 | 0.8122 | 0.561 |

## (b) regime_history diagnostic — does breadth add over BTC-only?

- regime rows: 71588 · usable (breadth+BTC non-NaN): 71588
- regime class counts: {'TRANSITION': 26011, 'CHOP': 23853, 'HIGH_VOLA': 16686, 'TREND_DOWN': 2755, 'TREND_UP': 2283}
- incremental logit (TREND_UP vs rest, chrono 70/30, n_test=21477): AUC BTC-only=0.8241 → BTC+breadth=0.6769 (Δ=-0.1471)

### Single-feature AUC (TREND_UP vs rest)

| feature | AUC |
|---|--:|
| brd_pct_above_ema200 | 0.5889 |
| brd_pct_above_ema50 | 0.5834 |
| brd_median_ret_7d | 0.5649 |
| brd_adv_decline_ratio | 0.4822 |
| brd_dispersion_vs_btc | 0.5643 |
| total3_ew_level | 0.5733 |
| total3_ew_dist_reg90d | 0.4796 |
| total3_ew_breakout | 0.5131 |
| total3_vw_level | 0.7015 |
| total3_vw_dist_reg90d | 0.6283 |
| total3_vw_breakout | 0.4865 |

## Caveats

- **Verdict basis**: §K6 stop-criterion — breadth must separate RUB-LONG OOS better than the existing BTC-only regime. A no-op is a valid, documented result; the builder stays as infra.
- **Survivorship**: breadth computed over active USDT-perps only; delisted coins missing.
- **TOTAL3 is a price proxy** (equal- and volume-weighted over perps ex BTC/ETH), not a
  market-cap index — prefer the scale-free dist_reg90d / breakout over the raw level.
- RUB signal_time is naive UTC; regime_history.ts is naive Bucharest → localized DST-aware. regime_history starts 2026-01-18, so RUB-LONG events before then carry no as-of regime baseline and drop from the head-to-head logit (reported as overlap n).
- CPU-check override: --skip-cpu-check=True (read-only, BELOW_NORMAL).
- Resume machinery: per-coin panel checkpoint every 25 coins to OS-temp state (survives watchdog kills); memory bounded, state removed on clean exit.