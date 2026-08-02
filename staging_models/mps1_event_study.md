# MPS1 — Max-Pain-Band-Touch Event Study (T-2026-KYT-9050-073)

Generated: 2026-08-02T20:37:28.938661+00:00 · status: complete
Universe: 527/527 symbols · events: 9,754 · controls: 50,567
Window: 2026-06-12 20:40:00+00:00 → 2026-08-02 19:30:00+00:00 (split 2026-07-08 08:05:00+00:00)

## VERDICT: **EDGE** (sides: up)

Gate (4h, per side, val AND test, n ≥ 200): net mean > 0 AND event gross mean > control gross mean.

## Event reversion returns (net of 0.10 % round-trip fee)

| side | half | 1h n / mean / t | 4h n / mean / t | 8h n / mean / t | 24h n / mean / t |
|---|---|---|---|---|---|
| up | val | 3054 / -0.005% / -0.0 | 3054 / +0.143% / 0.9 | 3054 / +0.252% / 1.2 | 3054 / +0.492% / 1.4 |
| up | test | 2153 / +0.052% / 0.4 | 2139 / +0.159% / 0.7 | 2124 / +0.412% / 1.5 | 2065 / +1.043% / 2.7 |
| down | val | 2683 / -0.105% / -1.2 | 2683 / -0.039% / -0.3 | 2683 / -0.013% / -0.1 | 2683 / -0.777% / -2.6 |
| down | test | 1857 / -0.132% / -1.1 | 1851 / -0.024% / -0.1 | 1840 / -0.021% / -0.1 | 1794 / -0.564% / -1.6 |

## Event reversion returns (gross)

| side | half | 1h n / mean / t | 4h n / mean / t | 8h n / mean / t | 24h n / mean / t |
|---|---|---|---|---|---|
| up | val | 3054 / +0.095% / 0.9 | 3054 / +0.243% / 1.6 | 3054 / +0.352% / 1.7 | 3054 / +0.592% / 1.7 |
| up | test | 2153 / +0.152% / 1.3 | 2139 / +0.259% / 1.2 | 2124 / +0.512% / 1.9 | 2065 / +1.143% / 2.9 |
| down | val | 2683 / -0.005% / -0.1 | 2683 / +0.061% / 0.4 | 2683 / +0.087% / 0.4 | 2683 / -0.677% / -2.3 |
| down | test | 1857 / -0.032% / -0.3 | 1851 / +0.076% / 0.4 | 1840 / +0.079% / 0.4 | 1794 / -0.464% / -1.3 |

## Control reversion returns (gross — fresh 24h extremes without a nearby band)

| side | half | 1h n / mean / t | 4h n / mean / t | 8h n / mean / t | 24h n / mean / t |
|---|---|---|---|---|---|
| up | val | 10690 / +0.047% / 1.8 | 10690 / +0.060% / 1.3 | 10690 / +0.075% / 1.3 | 10690 / +0.121% / 1.2 |
| up | test | 11291 / +0.073% / 3.6 | 11238 / +0.072% / 2.0 | 11173 / +0.157% / 3.4 | 10895 / +0.482% / 6.7 |
| down | val | 13802 / +0.103% / 6.5 | 13802 / +0.185% / 6.9 | 13802 / +0.201% / 6.0 | 13802 / +0.342% / 5.9 |
| down | test | 14749 / +0.024% / 1.9 | 14713 / +0.154% / 7.5 | 14635 / +0.141% / 5.3 | 14467 / +0.190% / 4.1 |

## Descriptive slices (net 4h returns; NOT part of the gate)

| slice | cell | val n / mean | test n / mean |
|---|---|---|---|
| density (up) | d<10% | 118 / +2.367% | 52 / -0.545% |
| density (up) | d10-25% | 2432 / +0.220% | 1508 / -0.031% |
| density (up) | d>=25% | 504 / -0.749% | 579 / +0.716% |
| oi-tier (up) | mega>=1B | 2 / +0.665% | 0 / — |
| oi-tier (up) | major>=100M | 15 / -0.118% | 3 / +0.282% |
| oi-tier (up) | mid>=10M | 669 / +0.026% | 463 / -0.268% |
| oi-tier (up) | tail | 2368 / +0.177% | 1673 / +0.277% |
| density (down) | d<10% | 172 / -1.148% | 83 / +1.144% |
| density (down) | d10-25% | 2000 / +0.040% | 1186 / -0.053% |
| density (down) | d>=25% | 511 / +0.027% | 582 / -0.133% |
| oi-tier (down) | mega>=1B | 1 / +1.286% | 0 / — |
| oi-tier (down) | major>=100M | 6 / +0.671% | 3 / +0.012% |
| oi-tier (down) | mid>=10M | 580 / -0.316% | 454 / -0.170% |
| oi-tier (down) | tail | 2096 / +0.036% | 1394 / +0.023% |

## Spread capture (events with both bands; first-touch, SL-first on ambiguity, net PnL)

| side | half | tol | win | loss | timeout | win-rate | mean net PnL |
|---|---|---|---|---|---|---|---|
| up | val | 0.5% | 135 | 2719 | 199 | 4.4% | -0.239% |
| up | val | 1.0% | 169 | 2561 | 323 | 5.5% | -0.162% |
| up | val | 2.0% | 244 | 2265 | 544 | 8.0% | -0.000% |
| up | val | — no opposite band: 1 | | | | | |
| up | test | 0.5% | 65 | 1917 | 170 | 3.0% | -0.451% |
| up | test | 1.0% | 82 | 1798 | 272 | 3.8% | -0.420% |
| up | test | 2.0% | 131 | 1562 | 459 | 6.1% | -0.273% |
| up | test | — no opposite band: 5 | | | | | |
| down | val | 0.5% | 90 | 2426 | 165 | 3.4% | -0.399% |
| down | val | 1.0% | 126 | 2284 | 271 | 4.7% | -0.317% |
| down | val | 2.0% | 190 | 1988 | 503 | 7.1% | -0.265% |
| down | val | — no opposite band: 2 | | | | | |
| down | test | 0.5% | 73 | 1625 | 156 | 3.9% | -0.285% |
| down | test | 1.0% | 95 | 1519 | 240 | 5.1% | -0.245% |
| down | test | 2.0% | 131 | 1323 | 400 | 7.1% | -0.170% |
| down | test | — no opposite band: 6 | | | | | |

## Honest limits

* ~7 weeks of oi_5m history = ONE market regime, in-sample only (T-007 lesson).
* Heatmap leverage mix / long-short split are assumptions (see engine header) — bands are estimates, not ground truth; no liquidation feed to calibrate against.
* Survivorship: coins.json = active USDT perps only (documented, not corrected).
* Overlap: 4h cooldown; horizons beyond 4h still overlap across events.
