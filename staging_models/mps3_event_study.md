# MPS3 - Near-Band Max-Pain Event Study, tiers 25/50/100 (T-2026-KYT-9050-081)

Generated: 2026-08-03T07:14:50.746339+00:00 · status: complete
Universe: 527/527 symbols · events: 34,763 · controls: 42,327
Window: 2026-06-12 20:40:00+00:00 → 2026-08-03 06:20:00+00:00 (split 2026-07-08 13:30:00+00:00)

## VERDICT: **EDGE** (sides: up, down)

Gate (4h, per side, val AND test, n ≥ 200): net mean > 0 AND event gross mean > control gross mean.

## Event reversion returns (net of 0.10 % round-trip fee)

| side | half | 1h n / mean / t | 4h n / mean / t | 8h n / mean / t | 24h n / mean / t |
|---|---|---|---|---|---|
| up | val | 9186 / +0.070% / 1.8 | 9186 / +0.198% / 3.0 | 9186 / +0.278% / 3.2 | 9186 / +0.448% / 3.0 |
| up | test | 6512 / -0.049% / -1.1 | 6498 / +0.022% / 0.3 | 6467 / +0.116% / 1.1 | 6294 / +0.400% / 2.5 |
| down | val | 10973 / +0.040% / 1.4 | 10973 / +0.071% / 1.2 | 10973 / +0.096% / 1.3 | 10973 / -0.010% / -0.1 |
| down | test | 8067 / -0.048% / -1.5 | 8011 / +0.092% / 1.7 | 7944 / +0.054% / 0.7 | 7801 / -0.085% / -0.7 |

## Event reversion returns (gross)

| side | half | 1h n / mean / t | 4h n / mean / t | 8h n / mean / t | 24h n / mean / t |
|---|---|---|---|---|---|
| up | val | 9186 / +0.170% / 4.3 | 9186 / +0.298% / 4.5 | 9186 / +0.378% / 4.3 | 9186 / +0.548% / 3.7 |
| up | test | 6512 / +0.051% / 1.2 | 6498 / +0.122% / 1.5 | 6467 / +0.216% / 2.1 | 6294 / +0.500% / 3.1 |
| down | val | 10973 / +0.140% / 4.9 | 10973 / +0.171% / 3.0 | 10973 / +0.196% / 2.6 | 10973 / +0.090% / 0.8 |
| down | test | 8067 / +0.052% / 1.6 | 8011 / +0.192% / 3.5 | 7944 / +0.154% / 2.1 | 7801 / +0.015% / 0.1 |

## Control reversion returns (gross — fresh 24h extremes without a nearby band)

| side | half | 1h n / mean / t | 4h n / mean / t | 8h n / mean / t | 24h n / mean / t |
|---|---|---|---|---|---|
| up | val | 8678 / +0.057% / 1.8 | 8678 / +0.056% / 1.1 | 8678 / +0.087% / 1.3 | 8678 / +0.078% / 0.7 |
| up | test | 9803 / +0.062% / 2.8 | 9764 / +0.081% / 2.0 | 9728 / +0.167% / 3.3 | 9477 / +0.463% / 5.8 |
| down | val | 11285 / +0.089% / 4.7 | 11285 / +0.167% / 5.4 | 11285 / +0.165% / 4.1 | 11285 / +0.310% / 4.7 |
| down | test | 12535 / +0.018% / 1.2 | 12414 / +0.134% / 5.7 | 12213 / +0.129% / 4.3 | 12017 / +0.100% / 1.9 |

## Descriptive slices (net 4h returns; NOT part of the gate)

| slice | cell | val n / mean | test n / mean |
|---|---|---|---|
| density (up) | d<10% | 98 / +0.038% | 45 / -0.890% |
| density (up) | d10-25% | 1979 / +0.594% | 873 / +0.157% |
| density (up) | d>=25% | 7109 / +0.090% | 5580 / +0.008% |
| oi-tier (up) | mega>=1B | 12 / -0.273% | 8 / +0.487% |
| oi-tier (up) | major>=100M | 68 / -0.233% | 22 / +0.193% |
| oi-tier (up) | mid>=10M | 1788 / +0.084% | 1248 / -0.330% |
| oi-tier (up) | tail | 7318 / +0.231% | 5220 / +0.105% |
| density (down) | d<10% | 315 / -1.221% | 115 / -0.273% |
| density (down) | d10-25% | 2388 / -0.016% | 1530 / -0.073% |
| density (down) | d>=25% | 8270 / +0.145% | 6366 / +0.138% |
| oi-tier (down) | mega>=1B | 8 / +0.460% | 5 / -0.214% |
| oi-tier (down) | major>=100M | 73 / +0.446% | 37 / -0.167% |
| oi-tier (down) | mid>=10M | 1974 / -0.050% | 1547 / +0.028% |
| oi-tier (down) | tail | 8918 / +0.095% | 6422 / +0.109% |

## Spread capture (events with both bands; first-touch, SL-first on ambiguity, net PnL)

| side | half | tol | win | loss | timeout | win-rate | mean net PnL |
|---|---|---|---|---|---|---|---|
| up | val | 0.5% | 842 | 8019 | 323 | 9.2% | -0.173% |
| up | val | 1.0% | 1205 | 7401 | 578 | 13.1% | -0.106% |
| up | val | 2.0% | 1797 | 6189 | 1198 | 19.6% | -0.002% |
| up | val | — no opposite band: 2 | | | | | |
| up | test | 0.5% | 493 | 5644 | 366 | 7.6% | -0.268% |
| up | test | 1.0% | 710 | 5122 | 671 | 10.9% | -0.194% |
| up | test | 2.0% | 1057 | 4190 | 1256 | 16.3% | -0.109% |
| up | test | — no opposite band: 21 | | | | | |
| down | val | 0.5% | 888 | 9695 | 371 | 8.1% | -0.182% |
| down | val | 1.0% | 1276 | 8923 | 755 | 11.6% | -0.115% |
| down | val | 2.0% | 1798 | 7505 | 1651 | 16.4% | -0.114% |
| down | val | — no opposite band: 19 | | | | | |
| down | test | 0.5% | 523 | 6952 | 560 | 6.5% | -0.237% |
| down | test | 1.0% | 775 | 6270 | 990 | 9.6% | -0.164% |
| down | test | 2.0% | 1107 | 5009 | 1919 | 13.8% | -0.112% |
| down | test | — no opposite band: 45 | | | | | |

## Honest limits

* ~7 weeks of oi_5m history = ONE market regime, in-sample only (T-007 lesson).
* Heatmap leverage mix / long-short split are assumptions (see engine header) — bands are estimates, not ground truth; no liquidation feed to calibrate against.
* Survivorship: coins.json = active USDT perps only (documented, not corrected).
* Overlap: 4h cooldown; horizons beyond 4h still overlap across events.
