# OI / forced-liquidation entry gate for bot 40 — verdict (T-2026-KYT-9050-094)

_generated 2026-08-04 · read-only study · `tools/oi_liq_gate_study.py` · population:
1264 posted+filled+closed mirrors of `trailing_positions` (2026-07-26 → 2026-08-04) ·
marks unlevered %-points (the levered channel view is ~20× these numbers)_

**Question (Michi, 2026-08-04):** bot 40 still takes too many SL hits; SLs go 100–200%
into the red (levered) while wins are stopped at 20–40%. Can an entry gate on open
interest (`oi_5m`, K9) or forced liquidations (`liq_events`, LQE1) keep those trades
out of the book?

## Verdict

1. **OI gate: NO EDGE — do not build.** Entry-time OI and implied-price deltas
   (1h/4h/24h, absolute and direction-signed) carry zero information about which
   mirror ends in a deep loss: AUC 0.455–0.498 across all five features (0.5 = coin
   flip), and every threshold sweep moves the book by noise-sized amounts in both
   directions. This holds pooled AND within the single worst leg (MIS1-72H LONG:
   AUC 0.47–0.53).
2. **Liquidation gate: NOT CONCLUDABLE YET.** Collector 41 (`!forceOrder@arr`) has
   been writing `liq_events` only since **2026-08-03** — one day of overlap with the
   live book. No retrospective test is possible. Re-run the same study with liq
   features once ≥ 3 weeks of overlap exist (follow-up task
   **T-2026-KYT-9050-095**, ~2026-08-24). Note the stream is throttled to 1
   order/s/symbol — usable for cluster/timing features, not for volume sums.
3. **The premise itself is stale: today's config is net positive.** The book splits
   cleanly at the TIME_STOP go-live (2026-07-28 14:00 UTC, `TIME_STOP_SINCE`):

   | Cohort | n | Σ mark | SL hits |
   |---|--:|--:|--:|
   | pre-timestop (launch, grandfathered) | 409 | **−620.0** | 69 |
   | post-timestop (= today's config) | 855 | **+161.7** | 42 |

   The −620 is the already-paid launch cohort: 2026-07-27 alone (first full live
   day, BTC −2.3%) produced 265 MIS1-72H-LONG closes at −641 with 60 SL hits —
   before the time-stop existed. Since the cutoff, MIS1-72H LONG is ~flat
   (+53 over 128 closes) and the SL rate is ~5% of closes (42/855, avg −7.2).
4. **The win/loss asymmetry Michi sees is real but structural, not a leak.** TRAIL
   wins average +2.2 unlevered (≈ +44% levered @20x) because the trail banks at
   activation 2% + 10% giveback; SL hits average −6.3 to −7.2 (≈ −130…−145%
   levered) because the mirror deliberately keeps the source signal's catastrophic
   S/R stop (A/B comparability with the hold arm). The T-052 hard-stop row
   already measured what capping that asymmetry costs: `Trail a2 + SL cap −5%`
   ~halves Σ net (37.9k → 21.9k) for a MaxDD gain — rejected then, and nothing
   in the live data since reverses that.

## Numbers

### Exit-channel decomposition (all 1264, unlevered)

| Exit | n | Ø | Σ |
|---|--:|--:|--:|
| TRAIL | 702 | +2.17 | +1526.1 |
| SOURCE_CLOSED | 283 | −3.76 | −1038.6 |
| TIME_STOP | 174 | −1.40 | −244.1 |
| SL_HIT | 111 | −6.34 | −704.0 |
| **net** | | | **−458.3** |

### OI feature separation (deep loss = mark ≤ −4)

AUC: `doi_1h` 0.498 · `doi_4h` 0.478 · `doi_24h` 0.455 · `dpx_1h_signed` 0.480 ·
`dpx_4h_signed` 0.463 (n≈1131–1144, 90% OI coverage). Medians differ between
outcomes only in the third decimal — there is no signature at entry time. All 18
gate variants (|ΔOI| thresholds, OI-flush, OI-spike, chase/fade on signed price)
skip 2–19% of entries and change the kept sum by −26…+46 points on a −458 book,
uncorrelated with the threshold — noise.

### The deterministic alternative also fails: SL-distance admission cap

The realized SL depth IS the entry→SL distance (fill-at-stop booking), known at
admission. But far stops correlate with the *winners* (median `sl_dist`: TRAIL
8.15% vs SL_HIT 6.18%) — trails need room to run. Every cap on the post-timestop
cohort throws away more trail profit than SL damage avoided (baseline +161.7):
cap 6% → kept +52.1, cap 8% → kept +113.4, cap 10% → kept +97.5. Do not build.

## Method + caveats

- Population excludes `ENTRY_NOT_FILLED` and shadow rows (no position existed at
  Cornix) and 7 `SOURCE_CLOSED` rows with NULL mark.
- OI features from `oi_5m` only: `open_interest` (contracts) for ΔOI;
  `oi_value_usdt / open_interest` as implied mark price for Δpx (no candle-table
  join needed). `merge_asof` backward, 20-min tolerance, `datetime64[ns]`
  (T-073 epoch trap).
- In-sample threshold sweeps on 9 days of live data — deliberately generous to
  the gate hypothesis. That the gates fail *even in-sample* is what makes the
  NO-EDGE verdict cheap and safe; no out-of-sample confirmation is required for
  a negative.
- The SL_HIT mark books fill-at-stop, no slippage — the optimistic edge
  (`40_trailing_close_bot.sl_exit_mark`). Real SL damage is slightly worse;
  this biases *against* the verdict, not for it.
- SOURCE_CLOSED (−3.76 avg) mixes fleet SL/TP/timeout exits; it is the hold-arm
  exit imported into the mirror book and is not addressable by an entry gate
  either (same entries, by construction).

## Follow-up

- **T-2026-KYT-9050-095**: re-run with liquidation features (pre-entry liq value
  against trade direction, 15/60-min windows, cluster distance) once `liq_events`
  has ≥ 3 weeks of overlap with the live book (~2026-08-24). Same script,
  `MIN_LIQ_DAYS` guard lifts automatically.
