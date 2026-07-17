# K5 · LIS1 — Post-Listing-Drift cohort study + fade-replay (T-2026-CU-9050-144)

_Generated 2026-07-17T01:14:51.390760+00:00 · read-only cohort study · fee/side 0.0005 (round-trip 0.0010) · status **complete**_

**VERDICT: fade-short-candidate (needs follow-up bot task)**

- cohort n: 152 coins · small-n flag: **False** · max fade-cell n: 152 (floor 15)

- **Minimal deliverable**: coin age < 180 days => no LONG — market-neutral forward-return median negative at this horizon (n>=10). _implementation = orchestrator/bot gating change => Michi decides (not in this study)_

## Acceptance Criteria (§K5, binary)

| # | criterion | met | how-verified |
|--:|---|:--:|---|
| 1 | Listing date via exchangeInfo onboardDate, cached to listing_onboard_dates.json | ✅ | source=cache (C:\Users\Michael\Documents\Kythera\.claude\worktrees\feat+t-2026-cu-9050-144\staging_models\listing_onboard_dates.json); cache_present=True |
| 2 | Network-failure fallback = first 1h candle proxy per coin | ✅ | proxy path coded; coins on proxy=0 |
| 3 | Cohort = onboardDate inside data window (post-floor) | ✅ | cohort_n=152, excluded_pre_floor=375 |
| 4 | Forward returns Day0→{7,30,90,180} ABSOLUTE and MARKET-NEUTRAL (−BTC) | ✅ | both columns populated; beta confound fixed via market-neutral |
| 5 | Distribution + median + % positive per horizon | ✅ | median/mean/pct_positive/p5/p95 per horizon below |
| 6 | Fade-replay day{3,7,14} × limit{+0%,+5%} SHORT via simulate_exit | ✅ | 6 cells, simulate_exit first-touch on 1h |
| 7 | Funding cost MANDATORY, correctly signed (SHORT credited +Σrate) | ✅ | net_with_funding = geo_net + 100·Σ funding_rate over (entry,exit] |
| 8 | Small-n honesty (n per cohort/horizon/cell; no faked significance) | ✅ | per-horizon n reported; small_n_flag=False |
| 9 | Survivorship (Rule 9) documented; as-of/closed candles only (R1) | ✅ | coins.json=active perps; read_candles include_forming=False |
| 10 | Resume/checkpoint machinery (temp state, --resume, RAM guard, peak-RSS) | ✅ | state in OS temp; peak_rss=460.4MB |

**Reuse-vs-Build verdict:** REUSE the exit/geometry/funding stack (simulate_exit + get_hvn_and_sr_levels + hvn_sr_trade_geometry + ensure_min_tp_distance + load_funding) and the tsmom_study resume machinery; BUILD only the listing-cohort layer (exchangeInfo onboardDate cache, forward-return + fade-replay harness). No new geometry/fee/funding math.

## Forward returns — Day 0 → horizon (absolute vs market-neutral)

Day 0 = first 1d candle at/after onboardDate. Market-neutral = coin return − BTC return over the same calendar window (beta confound fixed).

| horizon (d) | n | abs median% | abs mean% | abs %pos | mkt median% | mkt mean% | mkt %pos | mkt p5% | mkt p95% |
|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 7 | 152 | -10.0759 | -2.2689 | 0.3684 | -8.2637 | -0.6966 | 0.3553 | -43.4125 | 74.7601 |
| 30 | 146 | -27.7888 | 12.5931 | 0.2877 | -22.0158 | 18.6287 | 0.3014 | -59.2007 | 105.5986 |
| 90 | 137 | -53.0376 | -32.8217 | 0.1752 | -34.3061 | -15.8285 | 0.2482 | -62.6587 | 94.1465 |
| 180 | 112 | -64.222 | -35.6619 | 0.1339 | -34.0618 | -2.725 | 0.2411 | -56.2825 | 190.9494 |

_n shrinks at long horizons: a 180d return needs 180d of post-listing 1d candles. Reported explicitly, never extrapolated._

## Beta-adjusted drift (does drift survive removing BTC?)

| horizon (d) | n | abs median% | mkt median% | mkt %pos | beta flips sign |
|--:|--:|--:|--:|--:|:--:|
| 7 | 152 | -10.0759 | -8.2637 | 0.3553 | False |
| 30 | 146 | -27.7888 | -22.0158 | 0.3014 | False |
| 90 | 137 | -53.0376 | -34.3061 | 0.2482 | False |
| 180 | 112 | -64.222 | -34.0618 | 0.2411 | False |

## Fade-replay — SHORT, day{3,7,14} × limit{+0%,+5%}, with funding

net_with_funding = simulate_exit net (first-touch TP-vs-SL, round-trip taker fee) + SHORT funding credit (+Σ funding_rate over the hold, ×100). geo_only excludes funding.

| cell | n | net+fund avg% | net+fund median% | WR | geo-only avg% | net p5% | net p95% |
|---|--:|--:|--:|--:|--:|--:|--:|
| d14|l0.0 | 151 | -0.0572 | 3.55 | 0.6424 | 0.139 | -22.5023 | 15.2287 |
| d14|l0.05 | 124 | 0.7335 | 4.1966 | 0.5887 | 0.7967 | -20.1681 | 18.2594 |
| d3|l0.0 | 152 | 1.0735 | 4.7077 | 0.7039 | 1.1959 | -32.304 | 18.8945 |
| d3|l0.05 | 136 | 1.9392 | 6.7479 | 0.6765 | 1.9229 | -26.9803 | 18.8017 |
| d7|l0.0 | 152 | -1.1264 | 3.897 | 0.6184 | -0.9296 | -27.9712 | 15.8675 |
| d7|l0.05 | 129 | 0.0241 | 5.354 | 0.5969 | 0.1288 | -29.4689 | 18.267 |

Positive fade cells (avg net+funding > 0 at n≥floor): `d3|l0.05` (1.9392%, n=136), `d3|l0.0` (1.0735%, n=152), `d14|l0.05` (0.7335%, n=124), `d7|l0.05` (0.0241%, n=129)

## Population & caveats

- run status: complete · coins processed: 527 of 527 requested (universe 527)
- listing-date source: cache (C:\Users\Michael\Documents\Kythera\.claude\worktrees\feat+t-2026-cu-9050-144\staging_models\listing_onboard_dates.json); per-coin source counts: {'exchangeInfo': 152, 'first_candle_proxy': 0}
- coins excluded (onboardDate at/before ~1y candle retention floor, drift not observable): 375
- peak process RSS: 460.4 MB
- forward returns on 1d candles (00:00 UTC anchor); fade entries/exits on 1h candles
- fade geometry: get_hvn_and_sr_levels on the as-of listing→entry frame (≤95d), SHORT geometry, 3 published TPs, first-touch exit scan capped 60d
- **Funding sign**: a SHORT is CREDITED positive funding (longs pay shorts) ⇒ short funding PnL = +Σ funding_rate over settlements in (entry, exit]; fresh perps' extreme positive funding therefore HELPS the short (correctly added, not subtracted)
- **Survivorship bias (Rule 9)**: coins.json = ACTIVE USDT-perps; delisted/rug'd fresh listings are ABSENT ⇒ the cohort skews to survivors, biasing post-listing drift UPWARD (the worst listings vanish). Documented, not corrected.
- **Only closed candles (R1)**: read_candles(include_forming=False); returns and geometry are as-of (no lookahead).
- **Small n (§K5)**: ~40–60 listings/yr ⇒ n is small, especially at 90/180d. n is reported per horizon and per fade cell; the verdict flags small-n rather than claiming significance.
- CPU-check override: --skip-cpu-check=True (read-only BELOW_NORMAL job; the walkforward_sim guard would abort on the CPU-saturated VPS).