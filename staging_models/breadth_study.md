# K6 · BRD — market breadth/dispersion study (T-2026-CU-9050-140)

> **SMOKE — full run pending.** This artifact was produced with symbol/event caps to
> prove the builder + study run end to end. The full-universe report is deferred to the
> queue (Ein-Job-Regel: a second heavy study must not run while another is live).

_Generated 2026-07-16T13:14:35.982083+00:00 · read-only · limit_symbols=6 · 
max_events=300 · universe_loaded=6_

Breadth is a PRICE proxy over active USDT-perps (survivorship-biased); TOTAL3 has no real
market-cap weights — see core/breadth_features docstring.

## Builder output — daily breadth panel

- panel rows (days): 873
- panel span (UTC): 2024-02-24 00:00:00+00:00 .. 2026-07-15 00:00:00+00:00
- features emitted: brd_pct_above_ema200, brd_pct_above_ema50, brd_median_ret_7d, brd_adv_decline_ratio, brd_dispersion_vs_btc, total3_ew_level, total3_ew_dist_reg90d, total3_ew_breakout, total3_vw_level, total3_vw_dist_reg90d, total3_vw_breakout

## (a) RUB-LONG events vs breadth as-of

- RUB LONG events streamed: 300 (with as-of breadth: 300)
- overall: avg net PnL -0.3381% · WR 0.52

### Per-feature gradient (Spearman vs net_pnl_pct; sign must survive the chrono split)

| feature | Spearman all | val | test |
|---|--:|--:|--:|
| brd_pct_above_ema200 | 0.1656 | 0.3602 | -0.2197 |
| brd_pct_above_ema50 | 0.1282 | 0.3337 | -0.124 |
| brd_median_ret_7d | 0.0882 | 0.2125 | -0.032 |
| brd_adv_decline_ratio | 0.0015 | 0.0014 | -0.0375 |
| brd_dispersion_vs_btc | -0.2023 | -0.2208 | -0.1647 |
| total3_ew_level | 0.0856 | 0.3102 | -0.0293 |
| total3_ew_dist_reg90d | 0.1042 | 0.1746 | 0.0528 |
| total3_ew_breakout | -0.0961 | -0.1406 | None |
| total3_vw_level | 0.0836 | 0.3076 | -0.0333 |
| total3_vw_dist_reg90d | 0.022 | 0.2172 | -0.2076 |
| total3_vw_breakout | -0.0961 | -0.1406 | None |

### Top vs bottom tercile net-PnL expectancy

| feature | bottom n | bottom PnL% | bottom WR | top n | top PnL% | top WR |
|---|--:|--:|--:|--:|--:|--:|
| brd_pct_above_ema200 | 222 | -1.031 | 0.4505 | 228 | -0.0932 | 0.5526 |
| brd_pct_above_ema50 | 182 | -0.8997 | 0.4615 | 118 | 0.5281 | 0.6102 |
| brd_median_ret_7d | 102 | 0.113 | 0.5 | 100 | 0.9489 | 0.64 |
| brd_adv_decline_ratio | 135 | 0.1471 | 0.5185 | 154 | -0.8124 | 0.5065 |
| brd_dispersion_vs_btc | 101 | 1.5852 | 0.6733 | 100 | -1.0653 | 0.49 |
| total3_ew_level | 103 | -1.6254 | 0.4272 | 109 | 1.4317 | 0.6514 |
| total3_ew_dist_reg90d | 102 | -0.9409 | 0.451 | 103 | 0.8836 | 0.6117 |
| total3_ew_breakout | 299 | -0.3101 | 0.5217 | 300 | -0.3381 | 0.52 |
| total3_vw_level | 103 | -1.6254 | 0.4272 | 107 | 1.5982 | 0.6636 |
| total3_vw_dist_reg90d | 103 | 0.9658 | 0.6019 | 104 | 0.3308 | 0.5577 |
| total3_vw_breakout | 299 | -0.3101 | 0.5217 | 300 | -0.3381 | 0.52 |

## (b) regime_history diagnostic — does breadth add over BTC-only?

- regime rows: 800 · usable (breadth+BTC non-NaN): 800
- NOTE: no TREND_UP rows in the (smoke-capped) regime_history window — AUC undefined (single class); the full run over all regime_history includes TREND_UP.
- incremental logit (TREND_UP vs rest, chrono 70/30, n_test=240): AUC BTC-only=None → BTC+breadth=None (Δ=None)

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