# K9 harvest: OI event study — divergence, spike-fade, OI×funding (T-2026-KYT-9050-096)

_generated 2026-08-04 · read-only study · `tools/oi_event_study.py` · data: `oi_5m`
2026-06-12 → 2026-08-04 (~7.6 weeks, 5.83M points) + `funding_rates` (as-of, 8h grid) ·
universe: 234 symbols with median OI ≥ $3M · hourly as-of grid, 24h per-symbol cooldown ·
fees 0.10 %/round trip · thresholds pre-registered, no per-coin tuning_

**Question (Michi, 2026-08-04):** the three model ideas seeded with the K9 collector
(`docs/MODEL_CANDIDATES_SPEC_2026-07.md:416-419`) — now that OI history exists, do any
of them carry an edge?

## Verdict

| Mechanic | n | net/event 1h | 4h | 24h | Verdict |
|---|--:|--:|--:|--:|---|
| **DIVERGENCE · SHORT** (rally ≥2% on OI ≤ −2%) | 728 | **+0.27** (t=2.9) | **+0.40** (t=2.2) | +0.26 | **CANDIDATE — the one survivor** |
| DIVERGENCE · LONG (dump on falling OI) | 1644 | −0.07 | −0.02 | −0.26 | NO EDGE |
| SPIKE-FADE (fade move behind OI spike) | 487 | −0.38 | −0.46 | **−2.56** | **REFUTED** — fading fresh OI gets run over |
| OI×FUNDING (squeeze susceptibility) | 190 | −0.38 | −1.44 (t=−2.0) | −0.61 | **REFUTED** at these thresholds |

1. **The only survivor is the SHORT side of the divergence fade:** a ≥2% rally whose
   4h OI *fell* ≥2% (short-covering rally, no new money) mean-reverts. Strengthening
   the move filter to ≥3% raises it to **net +0.41/event @1h (t=3.2), +0.73 @4h
   (t=3.2), WR 58–61%, n=580, 8 of 9 weeks positive** — and the effect is monotone
   in threshold strictness (px≥3/oi≤−4: net +0.67 @1h, +0.88 @4h, n=215). This
   mirrors the fleet-wide realized audit: the edge is directional (short), not
   regime-bound.
2. **The LONG mirror is dead** across every variant (net ≤ 0, 24h clearly negative).
   A dump on falling OI does not bounce reliably.
3. **Spike-fade is refuted the interesting way:** median +0.98% @24h but mean −2.46%
   — fading a fresh OI build-up wins slightly more often than it loses and then gets
   run over catastrophically in the tail. Do NOT naively invert either: the mean–median
   gap is tail-driven, not a stable momentum signal.
4. **OI×funding squeeze is refuted at pre-registered thresholds** (OI ≥ 90th own-30d
   percentile, |funding| ≥ 5 bps/8h): the crowded side tends to *continue* over 4h
   (anti-side gross +1.34, t≈2.0, but n=190 and weekly-unstable — an observation,
   not a claim).

## Method

- **Hourly as-of grid** (merge_asof backward, 45-min staleness cap) — necessary
  because the collector's effective cadence degraded from 5m to ~10–30 min since
  mid-July (see data-quality note). Price = implied mark `oi_value_usdt /
  open_interest` — same rows as the OI itself, no candle joins.
- Strictly causal: features from points ≤ t, forward returns from points ≥ t+h,
  stale rows voided rather than filled (P0.12).
- Events deduped per symbol per mechanic with a 24h cooldown (first wins);
  baseline = full-grid forward returns of the same universe/period.
- Side split motivated by the fleet-wide directional-edge finding; the divergence
  side split was examined after the pooled run — the threshold *matrix* (4 variants,
  all SHORT-positive, monotone) and the 8/9-week stability are the robustness
  answer to that post-hoc concern.

## Caveats (why this is a candidate, not a deployment)

- **~7.6 weeks, one market regime.** The spec's own gate was ≥60d; we ran ~1 week
  early as a falsification pass. Two of three mechanics died — that part is settled
  cheaply. The survivor needs the re-run at ≥90d before any bot exists.
- **Implied-price returns, no execution model.** 0.10% RT fees, no slippage/spread;
  the 1h horizon on ~$3M+ books is realistic for the fleet's sizes, but a live arm
  would post limit-to-Cornix like everything else and inherit its slippage profile.
- **First backfill week (2026-06-14) was the single negative week** — the effect
  may be weaker in strong-trend tape; the current sample is mostly chop.

## Data-quality note (own follow-up)

`35_oi_collector` has degraded from the designed 5-min sweep to an effective
**10–18 min cadence since mid-July, ~30 min on 2026-08-04**, plus a 45h outage
2026-07-12→14 (restart night). The study is cadence-robust (hourly grid), but the
5m table is silently becoming a 15m table — tracked as **T-2026-KYT-9050-097**
(investigate collector slowdown; likely sweep pagination/backoff or fleet CPU).

## Recommendation

1. **No deployment now.** Keep collecting; re-run `tools/oi_event_study.py` at
   ≥90d history (~2026-09-10). The script is parameter-frozen — a survivor there
   is out-of-sample relative to today's thresholds.
2. If DIVERGENCE-SHORT survives the re-run: research bot in the K1–K8 pattern
   (shadow-only, `CH_NEW_IDEAS`, default-off gate, own model intent section) —
   own task, Michi-gated.
3. Fix the collector cadence first (T-097) — a 30-min effective grid would blur
   the 1h-horizon edge the study found.

## Addendum 2026-08-04 — regime conditioning (operator question: "does this only work because we are in a bear market?")

Same data, same frozen event definitions; post-hoc subgroup analysis. Regime =
BTC and BTCDOM (`BTCDOMUSDT` perp, alt-weakness index) 7d/24h trend from the
same hourly grid's implied price, as-of the event timestamp. Sides deduped
separately here (the main run's cooldown is per mechanic across both sides,
hence slightly different pooled n). All numbers net of 0.10 % RT, per event.

**First: the sample was NOT a bear market.** BTC net +0.2 % over the window,
weekly returns [−2.5, −7.4, +7.9, +0.7, +0.8, +1.1, −2.7, −0.1]; BTCDOM fell
−3.3 % (alts slightly outperforming). The edge was not earned on an alt-bleed
tape.

**DIVERGENCE-SHORT (px≥3 %, oi≤−2 %) by regime:**

| Regime at event | n | net 1h | net 4h | net 24h |
|---|--:|--:|--:|--:|
| BTC 7d < −2 % | 254 | +0.15 | **+0.85** (t=2.7) | +1.68 (t=2.2) |
| BTC 7d −2…+2 % | 215 | +0.70 (t=2.2) | **+1.23** (t=2.4) | +1.00 |
| BTC 7d > +2 % | 193 | −0.22 | −0.34 | +1.00 (t=0.8) |
| BTC 24h > +1 % (hot day) | 252 | +0.36 | **+1.02** (t=2.6) | +1.46 |
| BTCDOM 7d > +1 % | 197 | +0.07 | +0.72 (t=2.1) | +1.50 |
| BTCDOM 7d < −1 % ("altseason") | 241 | +0.24 | +0.52 | +1.50 |
| worst 2×2 cell: BTC up × DOM down | 196 | −0.13 | −0.11 | +1.25 |

Readings: (a) **the killer is sustained BTC uptrend, not dominance** — across
all BTCDOM regimes the edge stays positive, but in BTC-7d-up tape the 1h/4h
edge disappears; (b) **it disappears rather than inverts** — the worst cells
sit at ~0, not at losses, and the 24h horizon stays positive everywhere
(weakly significant); (c) **short-term heat helps, sustained trend hurts**:
the strongest 4h edge sits on BTC-24h-hot days — mechanically consistent with
a short-covering rally getting follow-through buying only when the higher-level
trend is intact.

**DIVERGENCE-LONG (dump ≥3 % on OI ≤ −2 %) by regime — no pocket.** Best cell
+0.56 @4h at WR 51 % (t=1.96, BTC-flat); in BTC-7d-up tape the LONG fade
*loses* (−0.45 @4h, −1.45 @24h, WR 46 %). The dump-fade is not a bull-market
hedge: both sides dislike up-tape, only SHORT has a real edge anywhere. LONG
stays dead.

**Pre-registered hypothesis for the ≥90d re-run (~2026-09-10):** gate
DIVERGENCE-SHORT on **BTC 7d ≤ +2 %** (as-of, causal). In-sample this keeps
~71 % of events and essentially all of the PnL. It is registered HERE, before
the re-run data exists — the re-run evaluates it out-of-sample and must not
tune it. Caveats: subgroups on 9 weeks of tape; a genuine multi-week bull
regime is underrepresented (~12 grid-days of BTC-7d-up) — that flank stays
open until the tape provides it.
