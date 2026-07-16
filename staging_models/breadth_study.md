# K6 · BRD — market breadth/dispersion study (T-2026-CU-9050-140)

> **SMOKE — full run pending.** This artifact was produced with symbol/event caps to
> prove the builder + study run end to end. The full-universe report is deferred to the
> queue (Ein-Job-Regel: a second heavy study must not run while another is live).

_Generated 2026-07-16T13:33:40.093324+00:00 · read-only · limit_symbols=3 · 
max_events=200 · universe_loaded=3_

Breadth is a PRICE proxy over active USDT-perps (survivorship-biased); TOTAL3 has no real
market-cap weights — see core/breadth_features docstring.

## Builder output — daily breadth panel

- panel rows (days): 873
- panel span (UTC): 2024-02-24 00:00:00+00:00 .. 2026-07-15 00:00:00+00:00
- features emitted: brd_pct_above_ema200, brd_pct_above_ema50, brd_median_ret_7d, brd_adv_decline_ratio, brd_dispersion_vs_btc, total3_ew_level, total3_ew_dist_reg90d, total3_ew_breakout, total3_vw_level, total3_vw_dist_reg90d, total3_vw_breakout

## (a) RUB-LONG events vs breadth as-of

- RUB LONG events streamed: 200 (with as-of breadth: 200)
- overall: avg net PnL -1.1883% · WR 0.465

### Per-feature gradient (Spearman vs net_pnl_pct; sign must survive the chrono split)

| feature | Spearman all | val | test |
|---|--:|--:|--:|
| brd_pct_above_ema200 | 0.2267 | 0.3453 | 0.2268 |
| brd_pct_above_ema50 | 0.2146 | 0.3269 | 0.074 |
| brd_median_ret_7d | 0.1752 | 0.2583 | 0.0931 |
| brd_adv_decline_ratio | 0.0531 | -0.0616 | 0.2289 |
| brd_dispersion_vs_btc | -0.1412 | -0.1416 | -0.141 |
| total3_ew_level | 0.1108 | 0.2263 | -0.0249 |
| total3_ew_dist_reg90d | 0.2069 | 0.0671 | 0.3481 |
| total3_ew_breakout | None | None | None |
| total3_vw_level | 0.1108 | 0.2263 | -0.0249 |
| total3_vw_dist_reg90d | 0.2069 | 0.0671 | 0.3481 |
| total3_vw_breakout | None | None | None |

### Top vs bottom tercile net-PnL expectancy

| feature | bottom n | bottom PnL% | bottom WR | top n | top PnL% | top WR |
|---|--:|--:|--:|--:|--:|--:|
| brd_pct_above_ema200 | 126 | -2.303 | 0.3651 | 74 | 0.7097 | 0.6351 |
| brd_pct_above_ema50 | 147 | -2.0114 | 0.3946 | 200 | -1.1883 | 0.465 |
| brd_median_ret_7d | 73 | -2.5621 | 0.3151 | 70 | 0.1234 | 0.6 |
| brd_adv_decline_ratio | 135 | -1.4898 | 0.4296 | 182 | -1.2404 | 0.4505 |
| brd_dispersion_vs_btc | 67 | -1.469 | 0.4328 | 70 | -2.5721 | 0.3571 |
| total3_ew_level | 67 | -2.2577 | 0.3881 | 67 | 0.0697 | 0.5672 |
| total3_ew_dist_reg90d | 70 | -3.7407 | 0.2714 | 67 | -1.0667 | 0.4776 |
| total3_ew_breakout | 200 | -1.1883 | 0.465 | 200 | -1.1883 | 0.465 |
| total3_vw_level | 67 | -2.2577 | 0.3881 | 67 | 0.0697 | 0.5672 |
| total3_vw_dist_reg90d | 70 | -3.7407 | 0.2714 | 67 | -1.0667 | 0.4776 |
| total3_vw_breakout | 200 | -1.1883 | 0.465 | 200 | -1.1883 | 0.465 |

## (b) regime_history diagnostic — does breadth add over BTC-only?

- regime rows: 500 · usable (breadth+BTC non-NaN): 500
- NOTE: no TREND_UP rows in the (smoke-capped) regime_history window — AUC undefined (single class); the full run over all regime_history includes TREND_UP.
- incremental logit (TREND_UP vs rest, chrono 70/30, n_test=150): AUC BTC-only=None → BTC+breadth=None (Δ=None)

### Single-feature AUC (TREND_UP vs rest)

| feature | AUC |
|---|--:|
| brd_pct_above_ema200 | None |
| brd_pct_above_ema50 | None |
| brd_median_ret_7d | None |
| brd_adv_decline_ratio | None |
| brd_dispersion_vs_btc | None |
| total3_ew_level | None |
| total3_ew_dist_reg90d | None |
| total3_ew_breakout | None |
| total3_vw_level | None |
| total3_vw_dist_reg90d | None |
| total3_vw_breakout | None |

## Caveats

- **SMOKE run**: caps make the numbers non-decisive; the stop-criterion verdict (§K6) is
  the FULL run's job. The builder stays as infra regardless of the study outcome.
- **Survivorship**: breadth computed over active USDT-perps only; delisted coins missing.
- **TOTAL3 is a price proxy** (equal- and volume-weighted over perps ex BTC/ETH), not a
  market-cap index — the level/regression/breakout are proxy-relative.
- RUB signal_time is naive UTC; regime_history.ts is naive Bucharest → localized DST-aware.