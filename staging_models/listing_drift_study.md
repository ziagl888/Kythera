# K5 · LIS1 — Post-Listing-Drift cohort study + fade-replay (T-2026-CU-9050-144)

_Generated 2026-07-16T23:53:21.792017+00:00 · read-only cohort study · fee/side 0.0005 (round-trip 0.0010) · status **partial (sampling cap)**_

**VERDICT: n-too-small — descriptive-only (No-op-Done)**

- cohort n: 10 coins · small-n flag: **True** · max fade-cell n: 10 (floor 15)

- **Minimal deliverable**: coin age < 180 days => no LONG — market-neutral forward-return median negative at this horizon (n>=10). _implementation = orchestrator/bot gating change => Michi decides (not in this study)_

## Acceptance Criteria (§K5, binary)

| # | criterion | met | how-verified |
|--:|---|:--:|---|
| 1 | Listing date via exchangeInfo onboardDate, cached to listing_onboard_dates.json | ✅ | source=cache (C:\Users\Michael\Documents\Kythera\.claude\worktrees\feat+t-2026-cu-9050-144\staging_models\listing_onboard_dates.json); cache_present=True |
| 2 | Network-failure fallback = first 1h candle proxy per coin | ✅ | proxy path coded; coins on proxy=0 |
| 3 | Cohort = onboardDate inside data window (post-floor) | ✅ | cohort_n=10, excluded_pre_floor=0 |
| 4 | Forward returns Day0→{7,30,90,180} ABSOLUTE and MARKET-NEUTRAL (−BTC) | ✅ | both columns populated; beta confound fixed via market-neutral |
| 5 | Distribution + median + % positive per horizon | ✅ | median/mean/pct_positive/p5/p95 per horizon below |
| 6 | Fade-replay day{3,7,14} × limit{+0%,+5%} SHORT via simulate_exit | ✅ | 6 cells, simulate_exit first-touch on 1h |
| 7 | Funding cost MANDATORY, correctly signed (SHORT credited +Σrate) | ✅ | net_with_funding = geo_net + 100·Σ funding_rate over (entry,exit] |
| 8 | Small-n honesty (n per cohort/horizon/cell; no faked significance) | ✅ | per-horizon n reported; small_n_flag=True |
| 9 | Survivorship (Rule 9) documented; as-of/closed candles only (R1) | ✅ | coins.json=active perps; read_candles include_forming=False |
| 10 | Resume/checkpoint machinery (temp state, --resume, RAM guard, peak-RSS) | ✅ | state in OS temp; peak_rss=148.1MB |

**Reuse-vs-Build verdict:** REUSE the exit/geometry/funding stack (simulate_exit + get_hvn_and_sr_levels + hvn_sr_trade_geometry + ensure_min_tp_distance + load_funding) and the tsmom_study resume machinery; BUILD only the listing-cohort layer (exchangeInfo onboardDate cache, forward-return + fade-replay harness). No new geometry/fee/funding math.

## Forward returns — Day 0 → horizon (absolute vs market-neutral)

Day 0 = first 1d candle at/after onboardDate. Market-neutral = coin return − BTC return over the same calendar window (beta confound fixed).

| horizon (d) | n | abs median% | abs mean% | abs %pos | mkt median% | mkt mean% | mkt %pos | mkt p5% | mkt p95% |
|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| 7 | 10 | -10.2948 | -5.5173 | 0.3 | -7.9957 | -5.6974 | 0.2 | -28.6806 | 30.9062 |
| 30 | 10 | -24.9272 | -11.5582 | 0.2 | -19.9846 | -6.9271 | 0.2 | -38.6492 | 56.8606 |
| 90 | 10 | -49.5914 | -36.0907 | 0.2 | -39.7544 | -28.4359 | 0.2 | -62.407 | 40.1265 |
| 180 | 10 | -63.7048 | -0.8489 | 0.2 | -37.1783 | 24.022 | 0.2 | -61.9714 | 299.7929 |

_n shrinks at long horizons: a 180d return needs 180d of post-listing 1d candles. Reported explicitly, never extrapolated._

## Beta-adjusted drift (does drift survive removing BTC?)

| horizon (d) | n | abs median% | mkt median% | mkt %pos | beta flips sign |
|--:|--:|--:|--:|--:|:--:|
| 7 | 10 | -10.2948 | -7.9957 | 0.2 | False |
| 30 | 10 | -24.9272 | -19.9846 | 0.2 | False |
| 90 | 10 | -49.5914 | -39.7544 | 0.2 | False |
| 180 | 10 | -63.7048 | -37.1783 | 0.2 | False |

## Fade-replay — SHORT, day{3,7,14} × limit{+0%,+5%}, with funding

net_with_funding = simulate_exit net (first-touch TP-vs-SL, round-trip taker fee) + SHORT funding credit (+Σ funding_rate over the hold, ×100). geo_only excludes funding.

| cell | n | net+fund avg% | net+fund median% | WR | geo-only avg% | net p5% | net p95% |
|---|--:|--:|--:|--:|--:|--:|--:|
| d14|l0.0 ⚠small-n | 10 | 1.5262 | 4.3166 | 0.7 | 1.3362 | -12.7363 | 12.1091 |
| d14|l0.05 ⚠small-n | 6 | 5.2842 | 6.3888 | 0.6667 | 4.9015 | -5.7132 | 16.997 |
| d3|l0.0 ⚠small-n | 10 | -6.9853 | 5.2263 | 0.6 | -6.7432 | -44.7343 | 9.6874 |
| d3|l0.05 ⚠small-n | 9 | -0.2493 | 7.3254 | 0.5556 | 0.0039 | -22.4761 | 13.5282 |
| d7|l0.0 ⚠small-n | 10 | 0.1614 | 4.8702 | 0.6 | 0.0196 | -14.1715 | 14.3899 |
| d7|l0.05 ⚠small-n | 8 | 6.4759 | 8.6809 | 0.75 | 6.5039 | -6.5429 | 16.0016 |

No fade cell has positive avg net-with-funding at n≥floor.

## Population & caveats

- run status: partial (sampling cap) · coins processed: 10 of 10 requested (universe 527)
- listing-date source: cache (C:\Users\Michael\Documents\Kythera\.claude\worktrees\feat+t-2026-cu-9050-144\staging_models\listing_onboard_dates.json); per-coin source counts: {'exchangeInfo': 10, 'first_candle_proxy': 0}
- coins excluded (onboardDate at/before ~1y candle retention floor, drift not observable): 0
- peak process RSS: 148.1 MB
- forward returns on 1d candles (00:00 UTC anchor); fade entries/exits on 1h candles
- fade geometry: get_hvn_and_sr_levels on the as-of listing→entry frame (≤95d), SHORT geometry, 3 published TPs, first-touch exit scan capped 60d
- **Funding sign**: a SHORT is CREDITED positive funding (longs pay shorts) ⇒ short funding PnL = +Σ funding_rate over settlements in (entry, exit]; fresh perps' extreme positive funding therefore HELPS the short (correctly added, not subtracted)
- **Survivorship bias (Rule 9)**: coins.json = ACTIVE USDT-perps; delisted/rug'd fresh listings are ABSENT ⇒ the cohort skews to survivors, biasing post-listing drift UPWARD (the worst listings vanish). Documented, not corrected.
- **Only closed candles (R1)**: read_candles(include_forming=False); returns and geometry are as-of (no lookahead).
- **Small n (§K5)**: ~40–60 listings/yr ⇒ n is small, especially at 90/180d. n is reported per horizon and per fade cell; the verdict flags small-n rather than claiming significance.
- CPU-check override: --skip-cpu-check=True (read-only BELOW_NORMAL job; the walkforward_sim guard would abort on the CPU-saturated VPS).
- ⚠ SAMPLING CAP (NOT a full run): ['ERAUSDT', 'TAUSDT', 'CVXUSDT', 'SLPUSDT', 'ZORAUSDT', 'ESPORTSUSDT', 'TREEUSDT', 'PLAYUSDT', 'PROVEUSDT', 'TOWNSUSDT']. Full universe run deferred to the orchestrator Ein-Job slot.